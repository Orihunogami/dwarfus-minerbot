"""Конфиг из переменных окружения (.env)."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# ключ шифрования паролей (Fernet). Сгенерировать:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY = os.environ.get("FERNET_KEY", "")

# PostgreSQL DSN, напр.: postgresql://gm:gmpass@localhost:5432/goldenminer
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ключ CoinMarketCap для курсов (опционально; нет ключа -> бот работает, но без $)
CMC_API_KEY = os.environ.get("CMC_API_KEY", "")

# GitHub токен для проверки версий (опционально; без него анонимный лимит ~60/час на IP)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# как часто проверять версии репозиториев, минут
VERSION_CHECK_MINUTES = int(os.environ.get("VERSION_CHECK_MINUTES", "60"))

# как часто collector опрашивает аккаунты, минут
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "10"))

# граница "дня" для расчёта доходности: смещение от UTC в часах (3 = МСК)
DAY_TZ_OFFSET_HOURS = int(os.environ.get("DAY_TZ_OFFSET_HOURS", "0"))


def validate() -> None:
    missing = [n for n in ("BOT_TOKEN", "FERNET_KEY", "DATABASE_URL") if not globals()[n]]
    if missing:
        raise SystemExit(
            f"Не заданы переменные окружения: {', '.join(missing)}. "
            f"Скопируй .env.example в .env и заполни."
        )
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "")
