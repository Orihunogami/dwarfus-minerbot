"""
Клиент HiveOS API (https://api2.hiveos.farm/api/v2). Риг-сторона модели.

Токен — админский (env HIVE_TOKEN), авторизация заголовком Bearer.
Per-GPU данные (хешрейт, мощность) лежат в детальном воркере
/farms/{id}/workers/{wid}: monitoring_stats объединяем по bus_number с gpu_info
(модель) и gpu_stats (мощность).

Чистые функции parse_*/worker_gpus/aggregate тестируются на образцах без сети.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import httpx

log = logging.getLogger("hive")

BASE = "https://api2.hiveos.farm/api/v2"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def parse_farms(payload: dict) -> list[dict]:
    out = []
    for f in payload.get("data", []):
        st = f.get("stats") or {}
        out.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "online": st.get("workers_online", 0),
            "total": st.get("workers_total", 0),
        })
    return out


def worker_gpus(detail: dict) -> list[dict]:
    """Детальный воркер -> список карт: {bus, model, coin, hash, power}.
    Монета и хеш берутся из miners_stats (там coin + bus_numbers + hashes),
    модель — из gpu_info, мощность — из gpu_stats; join по bus_number."""
    d = detail.get("data", detail)

    model_by_bus: dict[int, str] = {}
    for g in (d.get("gpu_info") or []):
        bn = g.get("bus_number")
        if bn is not None and g.get("brand") != "internal":
            model_by_bus[bn] = g.get("short_name") or g.get("model") or "?"

    power_by_bus: dict[int, float] = {}
    for g in (d.get("gpu_stats") or []):
        bn = g.get("bus_number")
        if bn is not None:
            power_by_bus[bn] = g.get("power") or 0

    gpus = []
    for hr in ((d.get("miners_stats") or {}).get("hashrates") or []):
        coin = hr.get("coin") or "?"
        buses = hr.get("bus_numbers") or []
        hashes = hr.get("hashes") or []
        for i, bn in enumerate(buses):
            gpus.append({
                "bus": bn,
                "model": model_by_bus.get(bn, "?"),
                "coin": coin,
                "hash": hashes[i] if i < len(hashes) else 0,
                "power": power_by_bus.get(bn, 0),
            })
    return gpus


def aggregate(worker_gpu_lists: list[list[dict]]) -> dict:
    """[[карты воркера], ...] -> {coin: {hash, power, gpus, models:{model:{hash,power,count}}}}."""
    out: dict = {}
    for gpus in worker_gpu_lists:
        for g in gpus:
            c = out.setdefault(g["coin"], {
                "hash": 0.0, "power": 0.0, "gpus": 0,
                "models": defaultdict(lambda: {"hash": 0.0, "power": 0.0, "count": 0}),
            })
            c["hash"] += g["hash"]
            c["power"] += g["power"]
            c["gpus"] += 1
            m = c["models"][g["model"]]
            m["hash"] += g["hash"]
            m["power"] += g["power"]
            m["count"] += 1
    # defaultdict -> обычный dict для предсказуемости
    for c in out.values():
        c["models"] = dict(c["models"])
    return out


# ---------- сеть ----------
async def fetch_farms(token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0, headers=_headers(token)) as c:
        r = await c.get(BASE + "/farms")
        r.raise_for_status()
        return parse_farms(r.json())


async def fetch_farm_gpus(token: str, farm_id: int) -> list[list[dict]]:
    """Карты по онлайн-воркерам фермы (каждый воркер — отдельным запросом деталей)."""
    out: list[list[dict]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=_headers(token)) as c:
        r = await c.get(BASE + f"/farms/{farm_id}/workers")
        r.raise_for_status()
        for w in r.json().get("data", []):
            if not (w.get("stats") or {}).get("online"):
                continue
            rd = await c.get(BASE + f"/farms/{farm_id}/workers/{w['id']}")
            if rd.status_code == 200:
                out.append(worker_gpus(rd.json()))
            else:
                log.warning("worker %s/%s -> HTTP %s", farm_id, w.get("id"), rd.status_code)
    return out
