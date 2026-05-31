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
from aiogram.types import Message, CallbackQuery
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import crypto
import collector
import keyboards
import coins
import prices
from providers import registry
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


def _fmt_account_stats(label: str, wallet: str, snap, income_today, usd_price=None) -> str:
    import json
    name = label or (wallet[:10] + "…")
    if snap is None:
        return f"💎 <b>{name}</b>\nещё нет данных — collector скоро соберёт"

    inc = f"+{income_today:.2f}" if income_today is not None else "копим данные"
    te = snap["today_est"] or 0
    te_usd = f" (${te * usd_price:.2f})" if usd_price is not None else ""
    overview = (
        f"Намайнено        {snap['mined']:>10.2f}\n"
        f"Заблокировано    {snap['locked']:>10.2f}\n"
        f"Доступно         {snap['transferable']:>10.2f}\n"
        f"Прогноз сегодня  {snap['today_est']:>10.2f}{te_usd}\n"
        f"Доход за сутки   {inc:>10}"
    )
    if usd_price is not None:
        worth = (snap["transferable"] or 0) * usd_price
        overview += (
            "\n──────────────────────────"
            f"\nКурс             {usd_price:>10.4f}$"
            f"\nДоступно в $     {worth:>10.2f}$"
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
        f"<pre>{overview}</pre>\n"
        f"⚡️ Хешрейт: local {snap['local_rate']:.0f} · real {snap['real_rate']:.0f} p/s\n"
        f"{dev_block}\n"
        f"🕒 обновлено {_ago(snap['captured_at'])}"
    )


# ---------- карточка и меню (общее для команд и кнопок) ----------
async def account_card(tg_id: int, acc_row, *, force_refresh: bool = False) -> str:
    """Текст карточки одного аккаунта: при необходимости делает живой запрос,
    считает доход за сутки и форматирует. acc_row нужен с полями id/wallet/label."""
    day_start = _day_start_ts()
    now = time.time()
    aid = acc_row["id"]
    snap = await db.latest_snapshot(tg_id, aid)
    if force_refresh or snap is None or (now - snap["captured_at"]) > STALE_SECONDS:
        full = await db.get_account(tg_id, aid)
        if full is not None:
            await collector.collect_account(full)
            snap = await db.latest_snapshot(tg_id, aid)
    income = None
    if snap is not None:
        base = await db.mined_at_day_start(tg_id, aid, day_start)
        if base is not None and snap["mined"] >= base:
            income = snap["mined"] - base
    usd = await prices.get_price(_coin_of(acc_row))
    return _fmt_account_stats(acc_row["label"], acc_row["wallet"], snap, income, usd_price=usd)


def _coin_of(acc_row):
    """Объект монеты (coins.Coin) для аккаунта или None."""
    p = registry.get(acc_row["provider_key"]) if "provider_key" in acc_row else None
    return coins.get(p.meta.coin) if p else None


def _coin_key_of(acc_row) -> str | None:
    c = _coin_of(acc_row)
    return c.key if c else None


def _wd_cap(acc_row) -> bool:
    """Умеет ли провайдер этого аккаунта выводы (показывать кнопку или нет)."""
    p = registry.get(acc_row["provider_key"]) if "provider_key" in acc_row else None
    return bool(p and p.capabilities.withdrawals)


async def show_home(tg_id: int):
    """Возвращает (text, markup) домашнего экрана. Пока монета/способ одни —
    эти уровни пропускаются: 0 аккаунтов -> подключить, 1 -> сразу карточка,
    больше -> список выбора."""
    rows = await db.list_accounts(tg_id)
    if not rows:
        return (
            "Бот мониторинга майнинга.\n\n"
            "Подключи первый аккаунт — дальше всё в кнопках.\n"
            "Данные видишь только ты, пароль хранится зашифрованным.",
            keyboards.home_no_accounts(),
        )
    if len(rows) == 1:
        text = await account_card(tg_id, rows[0])
        return text, keyboards.card_actions(
            rows[0]["id"], with_back=False, withdrawals=_wd_cap(rows[0]),
            coin_key=_coin_key_of(rows[0]),
        )
    return "Твои аккаунты — выбери:", keyboards.account_picker(rows)


async def _safe_edit(cq: CallbackQuery, text: str, markup) -> None:
    """Редактирует сообщение под кнопкой; глотает 'message is not modified' и т.п."""
    try:
        await cq.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        log.debug("edit_text skip: %s", e)


# ---------- commands ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await show_home(m.from_user.id)
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


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
    await status.edit_text("Готово, аккаунт подключён ✅\nСобираю данные…")
    log.info("user %s added account %s", m.from_user.id, acc_id)
    text, markup = await show_home(m.from_user.id)
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


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
    blocks = [await account_card(m.from_user.id, r) for r in rows]
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
            txs = await collector.withdrawals_for(acc)
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


# ---------- inline-кнопки ----------
@dp.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await show_home(cq.from_user.id)
    await _safe_edit(cq, text, markup)
    await cq.answer()


@dp.callback_query(F.data.startswith("acc:"))
async def cb_account(cq: CallbackQuery):
    aid = int(cq.data.split(":")[1])
    acc = await db.get_account(cq.from_user.id, aid)
    if acc is None:
        await cq.answer("Аккаунт не найден", show_alert=True)
        return
    await cq.answer("Обновляю…")
    text = await account_card(cq.from_user.id, acc, force_refresh=True)
    rows = await db.list_accounts(cq.from_user.id)
    await _safe_edit(
        cq, text,
        keyboards.card_actions(aid, with_back=len(rows) > 1, withdrawals=_wd_cap(acc),
                               coin_key=_coin_key_of(acc)),
    )


@dp.callback_query(F.data.startswith("wd:"))
async def cb_withdrawals(cq: CallbackQuery):
    aid = int(cq.data.split(":")[1])
    acc = await db.get_account(cq.from_user.id, aid)
    if acc is None:
        await cq.answer("Аккаунт не найден", show_alert=True)
        return
    await cq.answer("Запрашиваю выводы…")
    name = acc["label"] or (acc["wallet"][:10] + "…")
    try:
        txs = await collector.withdrawals_for(acc)
    except Exception as e:
        await _safe_edit(cq, f"💸 <b>{name}</b>\nне удалось получить: {e}",
                         keyboards.back_to(f"acc:{aid}"))
        return
    if not txs:
        body = f"💸 <b>{name}</b>\nвыводов пока не было"
    else:
        txs = sorted(txs, key=lambda t: t.get("timestamp", 0), reverse=True)[:5]
        lines = []
        for t in txs:
            date = time.strftime("%d.%m.%Y", time.localtime(t.get("timestamp", 0)))
            lines.append(
                f" • {t.get('amount', 0):.2f} (комиссия {t.get('fee', 0)}) "
                f"— {t.get('status')} — {date}"
            )
        body = f"💸 <b>{name}</b>\n" + "\n".join(lines)
    await _safe_edit(cq, body, keyboards.back_to(f"acc:{aid}"))


@dp.callback_query(F.data == "accounts")
async def cb_accounts(cq: CallbackQuery):
    rows = await db.list_accounts(cq.from_user.id)
    if not rows:
        await _safe_edit(cq, "Нет аккаунтов.", keyboards.home_no_accounts())
    else:
        await _safe_edit(cq, "Аккаунты — управление:", keyboards.accounts_manage(rows))
    await cq.answer()


@dp.callback_query(F.data.startswith("logout:"))
async def cb_logout(cq: CallbackQuery):
    aid = int(cq.data.split(":")[1])
    ok = await db.delete_account(cq.from_user.id, aid)
    await collector._drop_client(aid)
    await cq.answer("Аккаунт удалён" if ok else "Не найден", show_alert=not ok)
    text, markup = await show_home(cq.from_user.id)
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("coin:"))
async def cb_coin(cq: CallbackQuery):
    coin_key = cq.data.split(":", 1)[1]
    c = coins.get(coin_key)
    if c is None:
        await cq.answer("Монета не найдена", show_alert=True)
        return
    await cq.answer()
    usd = await prices.get_price(c)
    lines = [f"📦 <b>{c.name}</b>"]
    if c.price is not None:
        rate = f"${usd:.4f}" if usd is not None else "недоступен"
        lines.append(f"Курс: {rate}  (источник: {c.price.kind})")
    if c.repos:
        lines.append("\nРепозитории:")
        for r in c.repos:
            lines.append(f" • {r.kind} — {r.url}")
    else:
        lines.append("\nРепозитории не привязаны.")
    lines.append("\n<i>Управление репами и слежение за версиями — следующим шагом.</i>")
    await _safe_edit(cq, "\n".join(lines), keyboards.back_to("home"))


@dp.callback_query(F.data == "login")
async def cb_login(cq: CallbackQuery, state: FSMContext):
    await state.set_state(LoginFlow.wallet)
    await cq.message.answer("Пришли адрес кошелька (username с сайта).")
    await cq.answer()


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