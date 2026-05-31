"""
Фоновое слежение за версиями.

Ходит по репам с watch_enabled, тянет последнюю версию (versions.fetch_latest)
и, если она изменилась — пишет владельцу аккаунта. Первая фиксация версии
делается молча (чтобы не спамить при добавлении репы).
"""
from __future__ import annotations

import asyncio
import time
import logging

import config
import db
import coins
import versions

log = logging.getLogger("watcher")


async def check_all(bot) -> None:
    repos = await db._internal_all_watched_repos()
    if not repos:
        return
    token = getattr(config, "GITHUB_TOKEN", "") or None
    log.info("version check: %d реп", len(repos))
    for r in repos:
        latest = await versions.fetch_latest(r["url"], r["watch_mode"], token)
        await asyncio.sleep(1.0)  # бережём rate limit
        if not latest or not latest.get("version"):
            continue
        ver = latest["version"]
        link = latest.get("url")
        now = int(time.time())

        if r["last_version"] is None:
            # первая фиксация — запоминаем без уведомления
            await db._internal_set_repo_version(r["id"], ver, link, now)
            continue

        if ver != r["last_version"]:
            await _notify(bot, r, latest)
        await db._internal_set_repo_version(r["id"], ver, link, now)


async def _notify(bot, repo_row, latest: dict) -> None:
    c = coins.get(repo_row["coin_key"])
    cname = c.name if c else repo_row["coin_key"]
    pr = versions.parse_repo(repo_row["url"])
    repo_name = pr[1] if pr else repo_row["url"]
    text = (
        f"📦 <b>{cname}</b>: новая версия\n"
        f"<code>{repo_name}</code> — <b>{latest['version']}</b> ({latest.get('kind')})\n"
        f"было: {repo_row['last_version']}"
    )
    if latest.get("url"):
        text += f"\n{latest['url']}"
    try:
        await bot.send_message(repo_row["tg_id"], text, parse_mode="HTML")
    except Exception as e:
        log.warning("не смог написать tg=%s: %s", repo_row["tg_id"], e)
