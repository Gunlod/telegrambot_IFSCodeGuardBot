"""Read-only Sheets API access and atomic, last-known-good memory cache."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import re
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .config import Config

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
MAX_USER_ID = 2**52 - 1


class SheetFormatError(ValueError):
    """Refuse a malformed sheet rather than overwriting a valid cache."""


def parse_rows(rows: list[list[object]]) -> frozenset[int]:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        raise SheetFormatError("Missing header: expected numeric user ID and remarks in A1:B1.")
    headers = [str(cell).strip().casefold() for cell in rows[0]]
    if headers != ["numeric user id", "remarks"]:
        raise SheetFormatError("Expected numeric user ID and remarks in A1:B1.")
    ids = set()
    for row_number, row in enumerate(rows[1:], start=2):
        if not isinstance(row, list):
            raise SheetFormatError(f"Invalid row at row {row_number}.")
        if not row or row[0] is None or (isinstance(row[0], str) and not row[0].strip()):
            continue
        value = row[0]
        if type(value) is int:
            user_id = value
        elif type(value) is float and math.isfinite(value) and value.is_integer():
            user_id = int(value)
        elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value.strip()):
            user_id = int(value.strip())
        else:
            raise SheetFormatError(f"Invalid numeric user ID at row {row_number}.")
        if not 0 < user_id <= MAX_USER_ID:
            raise SheetFormatError(f"Out-of-range numeric user ID at row {row_number}.")
        ids.add(user_id)
    return frozenset(ids)


class SheetsSource:
    """Blocking reader; invoked only in a worker thread by BlacklistCache."""

    def __init__(self, config: Config):
        self.config = config

    def fetch(self) -> frozenset[int]:
        # Load lazily so a missing/temporarily broken key does not stop the bot.
        credentials = service_account.Credentials.from_service_account_file(
            str(self.config.service_account_file), scopes=SCOPES
        )
        worksheet = self.config.worksheet_name.replace("'", "''")
        cell_range = quote(f"'{worksheet}'!A:B", safe="")
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.config.spreadsheet_id}/values/{cell_range}"
        )
        with AuthorizedSession(
            credentials,
            refresh_timeout=self.config.request_timeout,
            max_refresh_attempts=1,
        ) as session:
            response = session.get(
                url,
                params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"},
                timeout=self.config.request_timeout,
            )
            response.raise_for_status()
            return parse_rows(response.json().get("values", []))


@dataclass(frozen=True)
class Snapshot:
    user_ids: frozenset[int] = frozenset()
    last_updated: datetime | None = None


class BlacklistCache:
    def __init__(self, fetch: Callable[[], frozenset[int]]):
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self.snapshot = Snapshot()
        self.last_refresh_ok: bool | None = None

    async def refresh(self) -> bool:
        # Serialize periodic/manual refreshes; joins continue using the old snapshot.
        async with self._lock:
            try:
                user_ids = await asyncio.to_thread(self._fetch)
            except Exception as exc:
                self.last_refresh_ok = False
                if self.snapshot.last_updated is None:
                    logger.error(
                        "BLACKLIST REFRESH FAILED NO USABLE CACHE: initial load has never "
                        "succeeded; blacklist enforcement unavailable error=%s",
                        json.dumps(str(exc), ensure_ascii=False),
                    )
                else:
                    logger.warning(
                        "BLACKLIST REFRESH FAILED Using previous cache entries=%d error=%s",
                        len(self.snapshot.user_ids), json.dumps(str(exc), ensure_ascii=False),
                    )
                return False
            self.snapshot = Snapshot(frozenset(user_ids), datetime.now(timezone.utc))
            self.last_refresh_ok = True
            logger.info("BLACKLIST REFRESH rows=%d", len(user_ids))
            return True
