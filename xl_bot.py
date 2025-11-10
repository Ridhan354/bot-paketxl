#!/usr/bin/env python3
"""Entry point for the XL Reminder bot."""
from __future__ import annotations

import logging

from dotenv import load_dotenv

from bot_paketxl import AppConfig
from bot_paketxl.app import XLReminderApp


def main() -> None:
    load_dotenv()
    config = AppConfig.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    app = XLReminderApp(config)
    app.ensure_token()
    application = app.build_application()
    app.setup_scheduler(application)
    logging.info("XL Reminder bot started. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
