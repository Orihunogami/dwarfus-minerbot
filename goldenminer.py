"""
Клиент GoldenMiner API (nockchain pool).

Авторизация:
    POST /api/login  {"username": wallet, "password": pwd}
        -> {"ok": true, "token": "<JWT>", "expiry": <unix>}   # ~7 дней
    GET  /api/<ep>/<wallet>   с заголовком  Authorization: Bearer <token>

Клиент логинится лениво, кеширует токен в памяти и перелогинивается,
когда токен близок к протуханию или сервер вернул 401.
Пароль и токен НИКОГДА не логируются.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("goldenminer")

BASE_URL = "https://goldenminer.net"
EXPIRY_SKEW = 120  # сек: перелогиниваемся за 2 мин до фактического протухания


class GoldenMinerError(Exception):
    pass


class AuthError(GoldenMinerError):
    """Неверный пароль / отказ в логине."""


@dataclass
class Snapshot:
    """Нормализованный срез статистики аккаунта на момент времени."""
    wallet: str
    captured_at: int          # unix, наш момент сбора
    mined: float
    locked: float
    transferable: float
    today_est: float
    local_rate: float
    real_rate: float
    devices_online: int
    devices: list             # [{name, local_ip, rate}, ...]
    raw: dict                 # сырьё на всякий случай


class GoldenMinerClient:
    def __init__(self, wallet: str, password: str, *, timeout: float = 15.0):
        self.wallet = wallet
        self._password = password
        self._token: str | None = None
        self._expiry: int = 0
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GoldenMinerClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---------- auth ----------
    async def _login(self) -> None:
        r = await self._http.post(
            "/api/login",
            json={"username": self.wallet, "password": self._password},
        )
        if r.status_code in (401, 403):
            raise AuthError(f"login rejected for {self.wallet[:8]}…")
        r.raise_for_status()
        data = r.json()
        if not data.get("ok") or "token" not in data:
            raise AuthError(f"unexpected login response: {data.get('message')}")
        self._token = data["token"]
        self._expiry = int(data.get("expiry", time.time() + 3600))
        log.info("logged in %s…, token valid until %s", self.wallet[:8], self._expiry)

    async def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._expiry - EXPIRY_SKEW:
            await self._login()

    async def _get(self, endpoint: str) -> dict | list:
        await self._ensure_token()
        url = f"/api/{endpoint}/{self.wallet}"
        headers = {"Authorization": f"Bearer {self._token}"}
        r = await self._http.get(url, headers=headers)
        if r.status_code == 401:                       # токен сдох раньше срока
            await self._login()
            r = await self._http.get(
                url, headers={"Authorization": f"Bearer {self._token}"}
            )
        r.raise_for_status()
        return r.json()

    # ---------- data endpoints ----------
    async def info(self) -> dict:
        return await self._get("info")

    async def power(self) -> dict:
        return await self._get("power")

    async def devices(self) -> dict:
        return await self._get("devices")

    async def transactions(self) -> list:
        return await self._get("transactions")

    # ---------- aggregate ----------
    async def snapshot(self) -> Snapshot:
        """Один срез: тянет info + power + devices и нормализует."""
        info = await self.info()
        power = await self.power()
        devices = await self.devices()
        dev_list = [
            {"name": d.get("name"), "local_ip": d.get("local_ip"), "rate": float(d.get("rate", 0))}
            for d in devices.get("devices", [])
        ]
        return Snapshot(
            wallet=self.wallet,
            captured_at=int(time.time()),
            mined=float(info.get("mined", 0)),
            locked=float(info.get("locked", 0)),
            transferable=float(info.get("transferable", 0)),
            today_est=float(info.get("today_est", 0)),
            local_rate=float(power.get("local_rate", 0)),
            real_rate=float(power.get("real_rate", 0)),
            devices_online=len(dev_list),
            devices=dev_list,
            raw={"info": info, "power": power, "devices": devices},
        )


# демо-запуск:  python goldenminer.py <wallet> <password>
if __name__ == "__main__":
    import asyncio, sys

    async def _main():
        wallet, pwd = sys.argv[1], sys.argv[2]
        async with GoldenMinerClient(wallet, pwd) as gm:
            snap = await gm.snapshot()
            print(f"mined={snap.mined}  transferable={snap.transferable}  "
                  f"today_est={snap.today_est}  real_rate={snap.real_rate}  "
                  f"devices_online={snap.devices_online}")

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())