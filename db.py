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

-- плагинная модель (аддитивно):
-- какой провайдер обслуживает аккаунт; существующие строки -> goldenminer
ALTER TABLE accounts  ADD COLUMN IF NOT EXISTS provider_key TEXT NOT NULL DEFAULT 'nockchain.goldenminer';
-- нормализованные поля снапшота (для будущего общего рендера; старые колонки остаются)
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS headline_json TEXT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS workers_json  TEXT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS raw_json      TEXT;

-- репозитории под слежение за версиями (per-user, как аккаунты)
CREATE TABLE IF NOT EXISTS repos (
    id            BIGSERIAL PRIMARY KEY,
    tg_id         BIGINT  NOT NULL,
    coin_key      TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'miner',
    watch_mode    TEXT    NOT NULL DEFAULT 'auto',
    watch_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_version  TEXT,
    last_url      TEXT,
    last_checked  BIGINT,
    created_at    BIGINT  NOT NULL,
    UNIQUE (tg_id, url)
);
CREATE INDEX IF NOT EXISTS idx_repos_tg    ON repos(tg_id);
CREATE INDEX IF NOT EXISTS idx_repos_watch ON repos(watch_enabled);

-- выбранные фермы HiveOS под наблюдение + цена электричества ($/кВт⋅ч) на ферму
CREATE TABLE IF NOT EXISTS hive_farms (
    id         BIGSERIAL PRIMARY KEY,
    tg_id      BIGINT NOT NULL,
    farm_id    BIGINT NOT NULL,
    name       TEXT,
    kwh_usd    DOUBLE PRECISION,
    created_at BIGINT NOT NULL,
    UNIQUE (tg_id, farm_id)
);
CREATE INDEX IF NOT EXISTS idx_hive_farms_tg ON hive_farms(tg_id);
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
            "SELECT id, wallet, label, is_active, provider_key FROM accounts "
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


async def first_snapshot_today(requester_tg_id: int, account_id: int,
                               day_start: int) -> asyncpg.Record | None:
    """Первый снапшот ПОСЛЕ начала дня (mined + captured_at) — для honest расчёта
    дохода за сегодня и пометки 'неполный день'."""
    async with _pool.acquire() as c:
        return await c.fetchrow(
            """SELECT s.mined, s.captured_at FROM snapshots s
               JOIN accounts a ON a.id = s.account_id
               WHERE s.account_id = $1 AND a.tg_id = $2 AND s.captured_at >= $3
               ORDER BY s.captured_at ASC LIMIT 1""",
            account_id, requester_tg_id, day_start,
        )


async def recent_points(requester_tg_id: int, account_id: int,
                        limit: int = 3000) -> list[asyncpg.Record]:
    """Последние срезы (captured_at, mined, today_est), новые первыми — для окна
    текущего захода по разрывам и расчёта дохода по дельте today_est."""
    async with _pool.acquire() as c:
        return await c.fetch(
            """SELECT s.captured_at, s.mined, s.today_est FROM snapshots s
               JOIN accounts a ON a.id = s.account_id
               WHERE s.account_id = $1 AND a.tg_id = $2
               ORDER BY s.captured_at DESC LIMIT $3""",
            account_id, requester_tg_id, limit,
        )


async def add_repo(tg_id: int, coin_key: str, url: str,
                   kind: str = "miner", watch_mode: str = "auto") -> int:
    async with _pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO repos (tg_id, coin_key, url, kind, watch_mode, created_at)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (tg_id, url)
               DO UPDATE SET coin_key = EXCLUDED.coin_key, kind = EXCLUDED.kind,
                             watch_enabled = TRUE
               RETURNING id""",
            tg_id, coin_key, url, kind, watch_mode, int(time.time()),
        )


async def list_repos(requester_tg_id: int, coin_key: str) -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM repos WHERE tg_id = $1 AND coin_key = $2 ORDER BY created_at",
            requester_tg_id, coin_key,
        )


async def get_repo(requester_tg_id: int, repo_id: int) -> asyncpg.Record | None:
    async with _pool.acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM repos WHERE id = $1 AND tg_id = $2", repo_id, requester_tg_id
        )


async def delete_repo(requester_tg_id: int, repo_id: int) -> bool:
    async with _pool.acquire() as c:
        res = await c.execute(
            "DELETE FROM repos WHERE id = $1 AND tg_id = $2", repo_id, requester_tg_id
        )
        return res.endswith("1")


async def set_repo_watch(requester_tg_id: int, repo_id: int, enabled: bool) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE repos SET watch_enabled = $3 WHERE id = $1 AND tg_id = $2",
            repo_id, requester_tg_id, enabled,
        )


async def set_repo_version(requester_tg_id: int, repo_id: int,
                           version: str | None, url: str | None, checked_at: int) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE repos SET last_version = $3, last_url = $4, last_checked = $5 "
            "WHERE id = $1 AND tg_id = $2",
            repo_id, requester_tg_id, version, url, checked_at,
        )


# ---------- фермы HiveOS (всегда с tg_id) ----------
async def toggle_hive_farm(tg_id: int, farm_id: int, name: str) -> bool:
    """Включить/выключить ферму под наблюдение. Возвращает новое состояние (True=включена)."""
    async with _pool.acquire() as c:
        exists = await c.fetchval(
            "SELECT 1 FROM hive_farms WHERE tg_id=$1 AND farm_id=$2", tg_id, farm_id)
        if exists:
            await c.execute("DELETE FROM hive_farms WHERE tg_id=$1 AND farm_id=$2", tg_id, farm_id)
            return False
        await c.execute(
            "INSERT INTO hive_farms (tg_id, farm_id, name, created_at) VALUES ($1,$2,$3,$4)",
            tg_id, farm_id, name, int(time.time()))
        return True


async def list_hive_farms(requester_tg_id: int) -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM hive_farms WHERE tg_id=$1 ORDER BY created_at", requester_tg_id)


async def list_hive_farm_ids(requester_tg_id: int) -> list[int]:
    rows = await list_hive_farms(requester_tg_id)
    return [r["farm_id"] for r in rows]


async def set_hive_kwh(tg_id: int, farm_id: int, kwh_usd: float | None) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE hive_farms SET kwh_usd=$3 WHERE tg_id=$1 AND farm_id=$2",
            tg_id, farm_id, kwh_usd)


# ---------- внутренние операции collector (без tg_id, не для пользователя) ----------
async def _internal_all_active_accounts() -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT id, tg_id, wallet, password_enc, provider_key FROM accounts WHERE is_active = TRUE"
        )


async def _internal_insert_snapshot(account_id: int, s: dict) -> None:
    import json
    async with _pool.acquire() as c:
        await c.execute(
            """INSERT INTO snapshots
               (account_id, captured_at, mined, locked, transferable, today_est,
                local_rate, real_rate, devices_online, devices_json,
                headline_json, workers_json, raw_json)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            account_id, s["captured_at"], s["mined"], s["locked"], s["transferable"],
            s["today_est"], s.get("local_rate"), s.get("real_rate"), s.get("devices_online"),
            json.dumps(s.get("devices", [])),
            s.get("headline_json"), s.get("workers_json"), s.get("raw_json"),
        )


async def _internal_set_inactive(account_id: int) -> None:
    async with _pool.acquire() as c:
        await c.execute("UPDATE accounts SET is_active = FALSE WHERE id = $1", account_id)


async def _internal_all_watched_repos() -> list[asyncpg.Record]:
    async with _pool.acquire() as c:
        return await c.fetch(
            "SELECT id, tg_id, coin_key, url, watch_mode, last_version "
            "FROM repos WHERE watch_enabled = TRUE"
        )


async def _internal_set_repo_version(repo_id: int, version: str | None,
                                     url: str | None, checked_at: int) -> None:
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE repos SET last_version = $2, last_url = $3, last_checked = $4 WHERE id = $1",
            repo_id, version, url, checked_at,
        )