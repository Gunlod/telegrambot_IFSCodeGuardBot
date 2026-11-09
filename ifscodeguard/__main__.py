"""Run with python -m ifscodeguard --config ./config.yaml."""

import argparse
import asyncio
import logging
import sys

from telegram import Update

from .bot import build_application
from .config import ConfigError, load_config
from .logging_setup import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="IFSCodeGuardBot Telegram group guard")
    parser.add_argument("--config", default="config.yaml", help="YAML config path (default: config.yaml)")
    args = parser.parse_args()
    if sys.version_info < (3, 13):
        parser.error("Python 3.13 or later is required.")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        configure_logging(config)
    except OSError:
        print("Cannot initialize log file. Check logging.file and directory permissions.", file=sys.stderr)
        return 2

    # Python 3.14+ does not implicitly create a current event loop for run_polling.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        application = build_application(config)
        application.run_polling(
            allowed_updates=[Update.CHAT_MEMBER, Update.MESSAGE],
            drop_pending_updates=False,
            bootstrap_retries=3,
        )
    except Exception as exc:
        logging.getLogger(__name__).critical("BOT STOPPED error=%r", str(exc))
        return 1
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
