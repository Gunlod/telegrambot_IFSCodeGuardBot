"""Validated YAML configuration. Paths are relative to the config file."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """An actionable configuration error without leaking configuration values."""


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    spreadsheet_id: str
    worksheet_name: str
    service_account_file: Path
    request_timeout: int = 15
    refresh_interval: int = 60
    log_level: str = "INFO"
    log_file: Path = Path("logs/ifscodeguard.log")
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {path}") from exc
    except yaml.YAMLError as exc:
        # YAML parser messages can include the secret-bearing source line.
        raise ConfigError("Invalid YAML syntax in config file.") from exc
    if not isinstance(data, dict):
        raise ConfigError("Config must contain a YAML mapping.")

    sections = {}
    for name in ("telegram", "google", "blacklist", "logging"):
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{name} must be a YAML mapping.")
        sections[name] = section

    def string(section: str, key: str, default: str | None = None) -> str:
        value = sections[section].get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{section}.{key} must be a non-empty string.")
        return value.strip()

    def positive_int(section: str, key: str, default: int) -> int:
        value = sections[section].get(key, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"{section}.{key} must be a positive integer.")
        return value

    def local_path(value: str) -> Path:
        result = Path(value).expanduser()
        return (path.parent / result).resolve()

    token = string("telegram", "bot_token")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ConfigError("telegram.bot_token must be a BotFather token.")
    url = urlparse(string("google", "spreadsheet_url"))
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?", url.path)
    if url.scheme != "https" or url.netloc != "docs.google.com" or not match:
        raise ConfigError("google.spreadsheet_url must be a Google Sheets HTTPS URL.")
    level = string("logging", "level", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    return Config(
        bot_token=token,
        spreadsheet_id=match.group(1),
        worksheet_name=string("google", "worksheet_name", "blacklist"),
        service_account_file=local_path(
            string("google", "service_account_file", "./secrets/service-account.json")
        ),
        request_timeout=positive_int("google", "request_timeout", 15),
        refresh_interval=positive_int("blacklist", "refresh_interval", 60),
        log_level=level,
        log_file=local_path(string("logging", "file", "./logs/ifscodeguard.log")),
        log_max_bytes=positive_int("logging", "max_bytes", 10 * 1024 * 1024),
        log_backup_count=positive_int("logging", "backup_count", 5),
    )
