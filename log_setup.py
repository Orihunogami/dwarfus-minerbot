"""Логирование: консоль + ротируемый файл logs/bot.log. Шумные либы приглушены."""
import os
import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # 5 МБ на файл, 7 архивов -> logs/bot.log, bot.log.1 ... bot.log.7
    fileh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=7, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # меньше шума от библиотек
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
