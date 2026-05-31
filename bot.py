"""
Telegram-бот GoldenMiner.

Команды:
  /start    — справка
  /login    — добавить аккаунт (спросит кош, потом пароль; сообщение с паролем удаляется)
  /stats    — текущий срез + доход за сегодня по твоим аккаунтам
  /accounts — список твоих аккаунтов
  /logout   — удалить аккаунт (и все его данные)

Изоляция: любой показ данных идёт через db.*-функции с твоим tg_id,
чужие аккаунты недоступны.
"""
from __future__ import annotations

import asyncio
import time
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import crypto
import collector
from goldenminer import GoldenMinerClient, AuthError
from log_setup import setup_logging

log = logging.getLogger("bot")

dp = Dispatcher()

# /stats делает живой запрос, если последний снапшот старше этого порога (сек)
STALE_SECONDS = 90


class LoginFlow(StatesGroup):
    wallet = State()
    password = State()


# ---------- helpers ----------
def _day_start_ts() -> int:
    off = config.DAY_TZ_OFFSET_HOURS * 3600
    local = time.time() + off
    midnight_local = local - (local % 86400)
    return int(midnight_local - off)


def _ago(captured_at: int) -> str:
    mins = int((time.time() - captured_at) / 60)
    return "только что" if mins < 1 else f"{mins} мин назад"


def _fmt_account_stats(label: str, wallet: str, snap, income_today) -> str:
    import json
    name = label or (wallet[:10] + "…")
    if snap is None:
        return f"💎 <b>{name}</b>\nещё нет данных — collector скоро соберёт"

    inc = f"+{income_today:.2f}" if income_today is not None else "копим данные"
    overview = (
        f"Намайнено        {snap['mined']:>10.2f}\n"
        f"Заблокировано    {snap['locked']:>10.2f}\n"
        f"Доступно         {snap['transferable']:>10.2f}\n"
        f"Прогноз сегодня  {snap['today_est']:>10.2f}\n"
        f"Доход за сутки   {inc:>10}"
    )

    try:
        devs = json.loads(snap["devices_json"]) if snap.get("devices_json") else []
    except Exception:
        devs = []
    if devs:
        lines = "\n".join(
            f" • {d['name']:<11} {d['rate']:>6.0f} p/s" for d in devs
        )
        dev_block = f"🖥 Онлайн ({len(devs)}):\n{lines}"
    else:
        dev_block = f"🖥 Устройств онлайн: {snap['devices_online']}"

    return (
        f"💎 <b>{name}</b>\n"
        f"<pre>{overview}</pre>"
        f"⚡️ Хешрейт: local {snap['local_rate']:.0f} · real {snap['real_rate']:.0f} p/s\n"
        f"{dev_block}\n"
        f"🕒 обновлено {_ago(snap['captured_at'])}"
    )


# ---------- commands ----------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Бот мониторинга GoldenMiner.\n\n"
        "/login — подключить аккаунт (кош + пароль)\n"
        "/stats — статистика и доход за сегодня\n"
        "/withdrawals — история выводов\n"
        "/accounts — твои аккаунты\n"
        "/logout — отключить аккаунт\n\n"
        "Данные видишь только ты. Пароль хранится в зашифрованном виде."
    )


@dp.message(Command("login"))
async def login_start(m: Message, state: FSMContext):
    await state.set_state(LoginFlow.wallet)
    await m.answer("Пришли адрес кошелька (username с сайта).")


@dp.message(LoginFlow.wallet)
async def login_wallet(m: Message, state: FSMContext):
    await state.update_data(wallet=m.text.strip())
    await state.set_state(LoginFlow.password)
    await m.answer(
        "Теперь пароль от аккаунта.\n"
        "⚠️ Сообщение с паролем я удалю сразу после получения."
    )


@dp.message(LoginFlow.password)
async def login_password(m: Message, state: FSMContext):
    password = m.text.strip()
    # сразу удаляем сообщение с паролем из чата
    try:
        await m.delete()
    except Exception:
        pass

    data = await state.get_data()
    wallet = data["wallet"]
    await state.clear()

    status = await m.answer("Проверяю логин…")

    # проверяем креды живым логином
    cli = GoldenMinerClient(wallet, password)
    try:
        await cli.snapshot()
    except AuthError:
        await status.edit_text("Не вышло войти — неверный кош или пароль. Попробуй /login снова.")
        await cli.aclose()
        return
    except Exception as e:
        await status.edit_text(f"Ошибка подключения: {e}. Попробуй позже.")
        await cli.aclose()
        return
    await cli.aclose()

    acc_id = await db.add_account(
        tg_id=m.from_user.id,
        wallet=wallet,
        password_enc=crypto.encrypt(password),
        label=None,
    )
    await status.edit_text(
        f"Готово, аккаунт подключён ✅\n"
        f"Сбор данных пошёл. Через пару минут смотри /stats."
    )
    log.info("user %s added account %s", m.from_user.id, acc_id)


@dp.message(Command("accounts"))
async def accounts(m: Message):
    rows = await db.list_accounts(m.from_user.id)
    if not rows:
        await m.answer("У тебя нет подключённых аккаунтов. /login чтобы добавить.")
        return
    lines = []
    for r in rows:
        flag = "" if r["is_active"] else " (неактивен — нужен /login заново)"
        lines.append(f"#{r['id']} — {r['wallet'][:14]}…{flag}")
    await m.answer("Твои аккаунты:\n" + "\n".join(lines))


@dp.message(Command("stats"))
async def stats(m: Message):
    rows = await db.list_accounts(m.from_user.id)
    if not rows:
        await m.answer("Нет аккаунтов. /login чтобы добавить.")
        return
    status = await m.answer("Собираю свежие данные…")
    day_start = _day_start_ts()
    now = time.time()
    blocks = []
    for r in rows:
        snap = await db.latest_snapshot(m.from_user.id, r["id"])
        # живой запрос, если данных ещё нет или они устарели
        if snap is None or (now - snap["captured_at"]) > STALE_SECONDS:
            acc = await db.get_account(m.from_user.id, r["id"])
            if acc is not None:
                await collector.collect_account(acc)
                snap = await db.latest_snapshot(m.from_user.id, r["id"])
        income = None
        if snap is not None:
            base = await db.mined_at_day_start(m.from_user.id, r["id"], day_start)
            if base is not None and snap["mined"] >= base:
                income = snap["mined"] - base
        blocks.append(_fmt_account_stats(r["label"], r["wallet"], snap, income))
    await status.edit_text("\n\n".join(blocks), parse_mode="HTML")


@dp.message(Command("withdrawals"))
async def withdrawals(m: Message):
    import time as _t
    rows = await db.list_accounts(m.from_user.id)
    if not rows:
        await m.answer("Нет аккаунтов. /login чтобы добавить.")
        return
    status = await m.answer("Запрашиваю историю выводов…")
    blocks = []
    for r in rows:
        acc = await db.get_account(m.from_user.id, r["id"])
        if acc is None:
            continue
        name = r["label"] or (r["wallet"][:10] + "…")
        try:
            cli = await collector._client_for(acc)
            txs = await cli.transactions()
        except Exception as e:
            blocks.append(f"💸 <b>{name}</b>\nне удалось получить: {e}")
            continue
        if not txs:
            blocks.append(f"💸 <b>{name}</b>\nвыводов пока не было")
            continue
        txs = sorted(txs, key=lambda t: t.get("timestamp", 0), reverse=True)[:5]
        lines = []
        for t in txs:
            date = _t.strftime("%d.%m.%Y", _t.localtime(t.get("timestamp", 0)))
            lines.append(
                f" • {t.get('amount', 0):.2f} (комиссия {t.get('fee', 0)}) "
                f"— {t.get('status')} — {date}"
            )
        blocks.append(f"💸 <b>{name}</b>\n" + "\n".join(lines))
    await status.edit_text("\n\n".join(blocks), parse_mode="HTML")


@dp.message(Command("logout"))
async def logout(m: Message):
    rows = await db.list_accounts(m.from_user.id)
    if not rows:
        await m.answer("Нет аккаунтов для удаления.")
        return
    # для MVP: если один аккаунт — удаляем его; иначе подсказываем формат
    parts = m.text.split()
    if len(parts) == 2 and parts[1].isdigit():
        acc_id = int(parts[1])
        ok = await db.delete_account(m.from_user.id, acc_id)
        await collector._drop_client(acc_id)
        await m.answer("Аккаунт и его данные удалены." if ok else "Аккаунт не найден среди твоих.")
    elif len(rows) == 1:
        acc_id = rows[0]["id"]
        await db.delete_account(m.from_user.id, acc_id)
        await collector._drop_client(acc_id)
        await m.answer("Аккаунт и его данные удалены.")
    else:
        ids = ", ".join(f"#{r['id']}" for r in rows)
        await m.answer(f"У тебя несколько аккаунтов ({ids}). Укажи: /logout <id>")


# ---------- entrypoint ----------
async def main():
    setup_logging()
    config.validate()
    await db.init()

    session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if config.TELEGRAM_PROXY else None
    bot = Bot(token=config.BOT_TOKEN, session=session)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collector.collect_all, "interval",
        minutes=config.POLL_MINUTES, next_run_time=None,
        max_instances=1, coalesce=True,
    )
    # первый сбор почти сразу
    scheduler.add_job(collector.collect_all, "date")
    scheduler.start()

    log.info("bot + collector запущены (опрос каждые %d мин)", config.POLL_MINUTES)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await collector.shutdown()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())