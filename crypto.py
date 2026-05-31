"""Шифрование паролей пользователей. Ключ — в env (FERNET_KEY), не в коде и не в БД."""
from cryptography.fernet import Fernet

import config

_f = Fernet(config.FERNET_KEY.encode()) if config.FERNET_KEY else None


def encrypt(plain: str) -> str:
    if _f is None:
        raise RuntimeError("FERNET_KEY не задан")
    return _f.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if _f is None:
        raise RuntimeError("FERNET_KEY не задан")
    return _f.decrypt(token.encode()).decode()
