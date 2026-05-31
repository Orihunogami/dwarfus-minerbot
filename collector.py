"""
Collector — фоновый сбор снапшотов.

- держит по одному GoldenMinerClient на аккаунт (кеш токена внутри клиента),
- раз в POLL_MINUTES опрашивает все активные аккаунты,
- ошибка одного аккаунта не валит цикл,
- неверный пароль (AuthError) -> аккаунт помечается неактивным.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

import config
import db
import crypto
from goldenminer import GoldenMinerClient, AuthError

log = logging.getLogger("collector")

# account_id -> клиент
_clients: dict[int, GoldenMinerClient] = {}


async def _client_for(acc) -> GoldenMinerClient:
    cli = _clients.get(acc["id"])
    if cli is None:
        cli = GoldenMinerClient(acc["wallet"], crypto.decrypt(acc["password_enc"]))
        _clients[acc["id"]] = cli
    return cli


async def _drop_client(account_id: int) -> None:
    cli = _clients.pop(account_id, None)
    if cli:
        await cli.aclose()


async def collect_account(acc) -> None:
    cli = await _client_for(acc)
    try:
        snap = await cli.snapshot()
    except AuthError:
        log.warning("auth failed for account %s — деактивирую", acc["id"])
        await db._internal_set_inactive(acc["id"])
        await _drop_client(acc["id"])
        return
    except Exception as e:
        log.warning("collect error account %s: %s", acc["id"], e)
        return
    await db._internal_insert_snapshot(acc["id"], asdict(snap))


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
    for acc_id in list(_clients):
        await _drop_client(acc_id)
