"""
Слой БД (PostgreSQL / asyncpg).

ИЗОЛЯЦИЯ ДАННЫХ — главное правило:
  каждый аккаунт принадлежит одному tg_id (владельцу).
  Все функции, обслуживающие запросы пользователя, ОБЯЗАТЕЛЬНО принимают
  requester_tg_id и фильтруют по нему. Достать чужой аккаунт через
  пользовательский путь невозможно. Без tg_id ходит только collector
  (внутренний процесс), функции с префиксом _internal_.
"""
from __future__ import annotations

import time
import asyncpg

import config

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            BIGSERIAL PRIMARY KEY,
    tg_id         BIGINT      NOT NULL,           -- владелец (telegram user id)
    wallet        TEXT        NOT NULL,
    password_enc  TEXT        NOT NULL,           -- Fernet-токен, не открытый пароль
    label         TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    BIGINT      NOT NULL,
    UNIQUE (tg_id, wallet)                        -- один и тот же кош у одного юзера один раз
);
CREATE INDEX IF NOT EXISTS idx_accounts_tg     ON accounts(tg_id);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);

CREATE TABLE IF NOT EXISTS snapshots (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    captured_at     BIGINT NOT NULL,
    mined           DOUBLE PRECISION,
    locked          DOUBLE PRECISION,
    transferable    DOUBLE PRECISION,
    today_est       DOUBLE PRECISION,
    local_rate      DOUBLE PRECISION,
    real_rate       DOUBLE PRECISION,
    devices_online  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_acc_ts ON snapshots(account_id, captured_at DESC);

-- миграция для уже созданных таблиц: детализация по устройствам (JSON-строка)
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS devices_json TEXT;
"""


async def init() -> None:
    global _pool
    _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as c:
        await c.execute(SCHEMA)


async def close() -> None:
    if _pool:
        await _pool.close()


# ---------- операции пользователя (всегда с tg_id) ----------
async def add_account(tg_id: int, wallet: str, password_enc: str, label: str | None) -> int:
    async with _pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO accounts (tg_id, wallet, password_enc, label, created_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (tg_id, wallet)
               DO UPDATE SET password_enc = EXCLUDED.password_enc,
                             is_active = TRUE
               RETURNING id""",
            tg_id, wallet, password_enc, label, int(time.time()),
        )


async def list_accounts(requester_tg_id: int) -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT id, wallet, label, is_active FROM accounts "
            "WHERE tg_id = $1 ORDER BY created_at",
            requester_tg_id,
        )


async def get_account(requester_tg_id: int, account_id: int) -> asyncpg.Record | None:
    """Вернёт аккаунт ТОЛЬКО если он принадлежит этому пользователю."""
    async with _pool.acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM accounts WHERE id = $1 AND tg_id = $2",
            account_id, requester_tg_id,
        )


async def delete_account(requester_tg_id: int, account_id: int) -> bool:
    async with _pool.acquire() as c:
        res = await c.execute(
            "DELETE FROM accounts WHERE id = $1 AND tg_id = $2",
            account_id, requester_tg_id,
        )
        return res.endswith("1")


async def latest_snapshot(requester_tg_id: int, account_id: int) -> asyncpg.Record | None:
    """Снапшот выдаётся только владельцу аккаунта (JOIN с проверкой tg_id)."""
    async with _pool.acquire() as c:
        return await c.fetchrow(
            """SELECT s.* FROM snapshots s
               JOIN accounts a ON a.id = s.account_id
               WHERE s.account_id = $1 AND a.tg_id = $2
               ORDER BY s.captured_at DESC LIMIT 1""",
            account_id, requester_tg_id,
        )


async def mined_at_day_start(requester_tg_id: int, account_id: int, day_start: int) -> float | None:
    """mined на последний снапшот ДО начала текущего дня — база для дневного дохода."""
    async with _pool.acquire() as c:
        return await c.fetchval(
            """SELECT s.mined FROM snapshots s
               JOIN accounts a ON a.id = s.account_id
               WHERE s.account_id = $1 AND a.tg_id = $2 AND s.captured_at < $3
               ORDER BY s.captured_at DESC LIMIT 1""",
            account_id, requester_tg_id, day_start,
        )


# ---------- внутренние операции collector (без tg_id, не для пользователя) ----------
async def _internal_all_active_accounts() -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT id, tg_id, wallet, password_enc FROM accounts WHERE is_active = TRUE"
        )


async def _internal_insert_snapshot(account_id: int, s: dict) -> None:
    import json
    async with _pool.acquire() as c:
        await c.execute(
            """INSERT INTO snapshots
               (account_id, captured_at, mined, locked, transferable, today_est,
                local_rate, real_rate, devices_online, devices_json)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            account_id, s["captured_at"], s["mined"], s["locked"], s["transferable"],
            s["today_est"], s["local_rate"], s["real_rate"], s["devices_online"],
            json.dumps(s.get("devices", [])),
        )


async def _internal_set_inactive(account_id: int) -> None:
    async with _pool.acquire() as c:
        await c.execute("UPDATE accounts SET is_active = FALSE WHERE id = $1", account_id)