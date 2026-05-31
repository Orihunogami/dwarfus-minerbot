"""
Провайдер nockchain.goldenminer — тонкая обёртка над уже готовым
GoldenMinerClient (top-level goldenminer.py). Сам клиент НЕ трогаем.

Адаптер делает три вещи:
  - достаёт из creds wallet/password и отдаёт их клиенту,
  - держит сессию (клиент кеширует JWT, как и раньше),
  - переводит родной Snapshot клиента в нормализованный конверт (to_snapshot).

Замечание про импорт: `from goldenminer import ...` — это АБСОЛЮТНЫЙ импорт
top-level модуля goldenminer.py, не этого файла (мы внутри пакета providers).
"""
from __future__ import annotations

from goldenminer import GoldenMinerClient, AuthError as _GMAuthError

from .base import (
    AuthError, AuthField, Balances, Capabilities, Metric, Provider,
    ProviderMeta, Snapshot, Worker,
)


def to_snapshot(gm) -> Snapshot:
    """Родной Snapshot GoldenMinerClient -> нормализованный конверт.
    Вынесено отдельно, чтобы можно было проверять нормализацию без сети."""
    return Snapshot(
        captured_at=gm.captured_at,
        balances=Balances(
            mined=gm.mined,
            locked=gm.locked,
            transferable=gm.transferable,
            today_est=gm.today_est,
        ),
        headline=[
            Metric("local", gm.local_rate, "p/s", "local"),
            Metric("real", gm.real_rate, "p/s", "real"),
        ],
        workers=[
            Worker(name=d.get("name") or "?", rate=float(d.get("rate", 0)),
                   extra={"local_ip": d.get("local_ip")})
            for d in gm.devices
        ],
        raw=gm.raw,
    )


class _GMSession:
    def __init__(self, creds: dict):
        self._cli = GoldenMinerClient(creds["wallet"], creds["password"])

    async def snapshot(self) -> Snapshot:
        try:
            gm = await self._cli.snapshot()
        except _GMAuthError as e:
            raise AuthError(str(e)) from e
        return to_snapshot(gm)

    async def withdrawals(self) -> list[dict]:
        try:
            return await self._cli.transactions()
        except _GMAuthError as e:
            raise AuthError(str(e)) from e

    async def aclose(self) -> None:
        await self._cli.aclose()


class GoldenMinerProvider(Provider):
    meta = ProviderMeta(
        key="nockchain.goldenminer",
        coin="nockchain",
        method="GoldenMiner (пул)",
        rate_unit="p/s",
    )
    capabilities = Capabilities(withdrawals=True, workers=True)

    def auth_schema(self) -> list[AuthField]:
        return [
            AuthField("wallet", "Адрес кошелька (username с сайта)"),
            AuthField("password", "Пароль", secret=True),
        ]

    def ident(self, creds: dict) -> str:
        return creds["wallet"]

    def session(self, creds: dict) -> _GMSession:
        return _GMSession(creds)
