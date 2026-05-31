"""
Collector — фоновый сбор снапшотов через плагинную модель.

- провайдер берётся из реестра по acc["provider_key"];
- на аккаунт держится одна сессия провайдера (внутри кешируется токен),
- раз в POLL_MINUTES опрашиваются все активные аккаунты,
- ошибка одного аккаунта не валит цикл,
- AuthError (неверные креды) -> аккаунт помечается неактивным.

Нормализованный Snapshot раскладывается в строку БД: универсальные балансы +
(пока) легаси-колонки GoldenMiner, которые читает текущий рендер бота, +
json-поля (headline/workers/raw) для будущего общего рендера.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

import config  # noqa: F401  (нужен другим модулям; здесь для единообразия окружения)
import db
import crypto
from providers import registry
from providers.base import AuthError, Snapshot

log = logging.getLogger("collector")

# account_id -> сессия провайдера
_sessions: dict[int, object] = {}


def _creds(acc) -> dict:
    """Креды для провайдера. Пока — легаси-форма GoldenMiner (wallet + пароль).
    Универсальное хранение creds (шифрованный JSON) появится вместе со
    следующим провайдером и общим логином."""
    return {"wallet": acc["wallet"], "password": crypto.decrypt(acc["password_enc"])}


async def _session_for(acc):
    s = _sessions.get(acc["id"])
    if s is None:
        prov = registry.get(acc["provider_key"])
        if prov is None:
            raise RuntimeError(f"нет провайдера для ключа {acc['provider_key']!r}")
        s = prov.session(_creds(acc))
        _sessions[acc["id"]] = s
    return s


async def _drop_client(account_id: int) -> None:
    """Имя сохранено ради bot.py (/logout). Закрывает сессию аккаунта."""
    s = _sessions.pop(account_id, None)
    if s:
        await s.aclose()


def _row_from_snapshot(snap: Snapshot) -> dict:
    """Нормализованный Snapshot -> строка для БД.
    Универсальные балансы + легаси-колонки GoldenMiner (их пока читает рендер) +
    json-поля для будущего общего рендера."""
    hv = {m.key: m.value for m in snap.headline}
    return {
        "captured_at": snap.captured_at,
        "mined": snap.balances.mined,
        "locked": snap.balances.locked,
        "transferable": snap.balances.transferable,
        "today_est": snap.balances.today_est,
        # легаси-совместимость с текущим рендером бота:
        "local_rate": hv.get("local"),
        "real_rate": hv.get("real"),
        "devices_online": len(snap.workers),
        "devices": [
            {"name": w.name, "local_ip": w.extra.get("local_ip"), "rate": w.rate}
            for w in snap.workers
        ],
        # нормализованные поля (на будущее):
        "headline_json": json.dumps([asdict(m) for m in snap.headline]),
        "workers_json": json.dumps([asdict(w) for w in snap.workers]),
        "raw_json": json.dumps(snap.raw, default=str),
    }


async def snapshot_for(acc) -> Snapshot:
    s = await _session_for(acc)
    return await s.snapshot()


async def withdrawals_for(acc) -> list[dict]:
    s = await _session_for(acc)
    return await s.withdrawals()


async def collect_account(acc) -> None:
    try:
        snap = await snapshot_for(acc)
    except AuthError:
        log.warning("auth failed for account %s — деактивирую", acc["id"])
        await db._internal_set_inactive(acc["id"])
        await _drop_client(acc["id"])
        return
    except Exception as e:
        log.warning("collect error account %s: %s", acc["id"], e)
        return
    await db._internal_insert_snapshot(acc["id"], _row_from_snapshot(snap))


async def collect_all() -> None:
    accounts = await db._internal_all_active_accounts()
    if not accounts:
        return
    log.info("collect cycle: %d аккаунтов", len(accounts))
    # последовательно, чтобы не молотить API пула пачкой
    for acc in accounts:
        await collect_account(acc)
        await asyncio.sleep(0.5)


async def shutdown() -> None:
    for acc_id in list(_sessions):
        await _drop_client(acc_id)
