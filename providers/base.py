"""
Базовые типы плагинной модели.

Плагин = провайдер = пара "монета.способ" (provider_key), напр.
"nockchain.goldenminer". Ядро бота и схема БД про конкретные пулы ничего
не знают — вся специфика живёт внутри плагина.

Каждый провайдер реализует контракт Provider:
    meta            — описание (монета, способ, единица хешрейта)
    capabilities    — что умеет (выводы, список воркеров)
    auth_schema()   — какие поля нужны для входа -> дженерик /login строит диалог
    ident(creds)    — устойчивый идентификатор аккаунта (для дедупа), напр. адрес
    session(creds)  — живое подключение к пулу (держит токен; его кеширует caller)
    login(creds)    — проверить креды (по умолчанию: удачный snapshot = ок)
    withdrawals()   — история выводов (если capabilities.withdrawals)

Креды — это dict (поле -> значение). Шифруются целиком как JSON
(crypto.encrypt(json.dumps(creds))), поэтому добавление нового поля входа
не требует миграции БД.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    pass


class AuthError(ProviderError):
    """Неверные креды / отказ во входе."""


@dataclass(frozen=True)
class AuthField:
    """Одно поле формы входа. secret=True -> бот удалит сообщение со значением."""
    key: str                       # ключ в creds, напр. "wallet"
    label: str                     # что показать человеку, напр. "Адрес кошелька"
    secret: bool = False
    placeholder: str | None = None


@dataclass(frozen=True)
class ProviderMeta:
    key: str                       # provider_key, напр. "nockchain.goldenminer"
    coin: str                      # ключ монеты, напр. "nockchain"
    method: str                    # человеку, напр. "GoldenMiner (пул)"
    rate_unit: str                 # единица хешрейта, напр. "p/s"


@dataclass(frozen=True)
class Capabilities:
    withdrawals: bool = False
    workers: bool = False


@dataclass
class Metric:
    """KPI для шапки карточки. Плагин сам решает, что вынести наверх —
    поэтому разные единицы (p/s, MH/s) живут тут, а не в схеме БД."""
    key: str
    value: float
    unit: str
    label: str | None = None


@dataclass
class Worker:
    name: str
    rate: float
    extra: dict = field(default_factory=dict)


@dataclass
class Balances:
    """Универсальные балансы — одинаковы для любой монеты, поэтому колонки в БД."""
    mined: float = 0.0
    locked: float = 0.0
    transferable: float = 0.0
    today_est: float = 0.0


@dataclass
class Snapshot:
    """
    Нормализованный конверт — одинаковая форма для любого провайдера:
        balances  — универсальные числа (станут колонками),
        headline  — KPI для шапки (специфика единиц живёт здесь),
        workers   — список воркеров/устройств,
        raw       — сырой ответ пула как есть (для отладки и будущих полей).
    """
    captured_at: int
    balances: Balances
    headline: list[Metric] = field(default_factory=list)
    workers: list[Worker] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class Session(Protocol):
    """Живое подключение к пулу для одного аккаунта. Держит токен внутри,
    поэтому caller (коллектор) кеширует сессию, а не пересоздаёт на каждый опрос."""
    async def snapshot(self) -> Snapshot: ...
    async def withdrawals(self) -> list[dict]: ...
    async def aclose(self) -> None: ...


class Provider:
    """Контракт плагина. Конкретные провайдеры наследуют и переопределяют."""
    meta: ProviderMeta
    capabilities: Capabilities = Capabilities()

    def auth_schema(self) -> list[AuthField]:
        raise NotImplementedError

    def ident(self, creds: dict) -> str:
        raise NotImplementedError

    def session(self, creds: dict) -> Session:
        raise NotImplementedError

    async def login(self, creds: dict) -> None:
        """По умолчанию: удачный snapshot = валидные креды."""
        s = self.session(creds)
        try:
            await s.snapshot()
        finally:
            await s.aclose()

    async def withdrawals(self, creds: dict) -> list[dict]:
        if not self.capabilities.withdrawals:
            return []
        s = self.session(creds)
        try:
            return await s.withdrawals()
        finally:
            await s.aclose()
