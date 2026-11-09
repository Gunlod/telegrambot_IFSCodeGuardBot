"""Rotating UTF-8 logs to stdout and disk, with Telegram token redaction."""

import logging
from logging.handlers import RotatingFileHandler
import re
import sys

from .config import Config


class RedactingFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record).replace(self.token, "[REDACTED]")
        return re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)


def configure_logging(config: Config) -> None:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    formatter = RedactingFormatter(config.bot_token)
    handlers = [logging.StreamHandler(sys.stdout), file_handler]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=config.log_level, handlers=handlers, force=True)
    # HTTP request URLs include the bot token. Redaction also covers exceptions.
    for name in ("httpx", "httpcore", "google.auth", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
