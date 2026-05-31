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
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import crypto
import collector
import keyboards
import coins
import prices
import versions
import watcher
import hive
import earnings
from providers import registry
from goldenminer import GoldenMinerClient, AuthError
from log_setup import setup_logging

log = logging.getLogger("bot")
auth_log = logging.getLogger("auth")


def _who(u) -> str:
    """Строка для журнала auth: id, username, имя."""
    uname = f"@{u.username}" if u.username else "—"
    return f"tg_id={u.id} {uname} ({u.full_name})"

dp = Dispatcher()

# /stats делает живой запрос, если последний снапшот старше этого порога (сек)
STALE_SECONDS = 90


class LoginFlow(StatesGroup):
    wallet = State()
    password = State()


class AddRepo(StatesGroup):
    url = State()


class SetKwh(StatesGroup):
    value = State()


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
        text, markup = (
            "Бот мониторинга майнинга.\n\n"
            "Подключи первый аккаунт — дальше всё в кнопках.\n"
            "Данные видишь только ты, пароль хранится зашифрованным.",
            keyboards.home_no_accounts(),
        )
    elif len(rows) == 1:
        text = await account_card(tg_id, rows[0])
        markup = keyboards.card_actions(
            rows[0]["id"], with_back=False, withdrawals=_wd_cap(rows[0]),
            coin_key=_coin_key_of(rows[0]),
        )
    else:
        text, markup = "Твои аккаунты — выбери:", keyboards.account_picker(rows)

    if getattr(config, "HIVE_TOKEN", ""):
        markup.inline_keyboard.append(
            [InlineKeyboardButton(text="⛏ Фермы (Hive)", callback_data="farms")]
        )
    return text, markup


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
    auth_log.info("start | %s", _who(m.from_user))
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
    auth_log.info("add_account | %s | acc=%s", _who(m.from_user), acc_id)
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
    if ok:
        auth_log.info("logout | %s | acc=%s", _who(cq.from_user), aid)
    await cq.answer("Аккаунт удалён" if ok else "Не найден", show_alert=not ok)
    text, markup = await show_home(cq.from_user.id)
    await _safe_edit(cq, text, markup)


async def build_coin_view(tg_id: int, coin_key: str):
    """(text, markup) экрана монеты: курс + список реп пользователя с версиями."""
    c = coins.get(coin_key)
    if c is None:
        return None, None
    usd = await prices.get_price(c)
    repos = await db.list_repos(tg_id, coin_key)
    lines = [f"📦 <b>{c.name}</b>"]
    if c.price is not None:
        rate = f"${usd:.4f}" if usd is not None else "недоступен"
        lines.append(f"Курс: {rate}  (источник: {c.price.kind})")

    # риг-сторона: Hive
    token = getattr(config, "HIVE_TOKEN", "") or None
    if token and c.hive_symbol:
        farm_ids = await db.list_hive_farm_ids(tg_id)
        if farm_ids:
            try:
                agg = await hive.farms_aggregate(token, farm_ids)
            except Exception:
                agg = {}
            rd = agg.get(c.hive_symbol)
            if rd:
                lines.append(f"Риги: {rd['gpus']} карт онлайн · {rd['power']:.0f} Вт")
                for model, m in rd["models"].items():
                    lines.append(f"  {model} ×{m['count']}")
    lines.append("")
    if repos:
        lines.append("Репозитории (👁 — на слежении):")
        for r in repos:
            name = r["url"].rstrip("/").split("/")[-1]
            ver = r["last_version"] or "—"
            eye = "👁" if r["watch_enabled"] else "🔕"
            lines.append(f" {eye} <b>{name}</b> ({r['kind']}): {ver}")
    else:
        lines.append("Репозиториев пока нет — добавь кнопкой ниже.")
    has_suggested = bool(c.repos)
    return "\n".join(lines), keyboards.coin_screen(coin_key, repos, has_suggested=has_suggested)


@dp.callback_query(F.data.startswith("coin:"))
async def cb_coin(cq: CallbackQuery):
    coin_key = cq.data.split(":", 1)[1]
    text, markup = await build_coin_view(cq.from_user.id, coin_key)
    if text is None:
        await cq.answer("Монета не найдена", show_alert=True)
        return
    await cq.answer()
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("addrepo:"))
async def cb_addrepo(cq: CallbackQuery, state: FSMContext):
    coin_key = cq.data.split(":", 1)[1]
    await state.set_state(AddRepo.url)
    await state.update_data(coin_key=coin_key)
    await cq.message.answer("Пришли ссылку на GitHub-репозиторий (https://github.com/owner/repo).")
    await cq.answer()


@dp.message(AddRepo.url)
async def addrepo_url(m: Message, state: FSMContext):
    url = m.text.strip()
    if versions.parse_repo(url) is None:
        await m.answer("Это не похоже на ссылку GitHub. Пришли вида https://github.com/owner/repo "
                       "или /start чтобы отменить.")
        return
    data = await state.get_data()
    coin_key = data.get("coin_key")
    await state.clear()

    kind = "wallet" if "wallet" in url.lower() else "miner"
    repo_id = await db.add_repo(m.from_user.id, coin_key, url, kind=kind)
    # фиксируем текущую версию молча, чтобы не прислать «новая версия» сразу после добавления
    latest = await versions.fetch_latest(url, "auto", getattr(config, "GITHUB_TOKEN", "") or None)
    if latest and latest.get("version"):
        await db.set_repo_version(m.from_user.id, repo_id, latest["version"],
                                  latest.get("url"), int(time.time()))
    await m.answer("Репозиторий добавлен, слежение включено ✅")
    text, markup = await build_coin_view(m.from_user.id, coin_key)
    if text:
        await m.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data.startswith("seedrepo:"))
async def cb_seedrepo(cq: CallbackQuery):
    coin_key = cq.data.split(":", 1)[1]
    c = coins.get(coin_key)
    if not c or not c.repos:
        await cq.answer("Нет рекомендованной репы", show_alert=True)
        return
    for r in c.repos:
        rid = await db.add_repo(cq.from_user.id, coin_key, r.url, kind=r.kind, watch_mode=r.watch)
        latest = await versions.fetch_latest(r.url, r.watch, getattr(config, "GITHUB_TOKEN", "") or None)
        if latest and latest.get("version"):
            await db.set_repo_version(cq.from_user.id, rid, latest["version"],
                                      latest.get("url"), int(time.time()))
    await cq.answer("Добавлено")
    text, markup = await build_coin_view(cq.from_user.id, coin_key)
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("wrepo:"))
async def cb_wrepo(cq: CallbackQuery):
    rid = int(cq.data.split(":")[1])
    repo = await db.get_repo(cq.from_user.id, rid)
    if repo is None:
        await cq.answer("Репа не найдена", show_alert=True)
        return
    await db.set_repo_watch(cq.from_user.id, rid, not repo["watch_enabled"])
    await cq.answer("Слежение выключено" if repo["watch_enabled"] else "Слежение включено")
    text, markup = await build_coin_view(cq.from_user.id, repo["coin_key"])
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("rmrepo:"))
async def cb_rmrepo(cq: CallbackQuery):
    rid = int(cq.data.split(":")[1])
    repo = await db.get_repo(cq.from_user.id, rid)
    if repo is None:
        await cq.answer("Репа не найдена", show_alert=True)
        return
    coin_key = repo["coin_key"]
    await db.delete_repo(cq.from_user.id, rid)
    await cq.answer("Удалена")
    text, markup = await build_coin_view(cq.from_user.id, coin_key)
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("chkcoin:"))
async def cb_chkcoin(cq: CallbackQuery):
    coin_key = cq.data.split(":", 1)[1]
    repos = await db.list_repos(cq.from_user.id, coin_key)
    if not repos:
        await cq.answer("Нет реп для проверки", show_alert=True)
        return
    await cq.answer("Проверяю…")
    token = getattr(config, "GITHUB_TOKEN", "") or None
    lines = ["🔍 <b>Проверка версий</b>"]
    for r in repos:
        name = r["url"].rstrip("/").split("/")[-1]
        latest = await versions.fetch_latest(r["url"], r["watch_mode"], token)
        if not latest or not latest.get("version"):
            lines.append(f" • {name}: не удалось проверить")
            continue
        ver = latest["version"]
        mark = " 🆕" if r["last_version"] and ver != r["last_version"] else ""
        lines.append(f" • {name}: {ver} ({latest.get('kind')}){mark}")
        await db.set_repo_version(cq.from_user.id, r["id"], ver, latest.get("url"), int(time.time()))
    await _safe_edit(cq, "\n".join(lines), keyboards.back_to(f"coin:{coin_key}"))


async def build_farms_view(tg_id: int):
    token = getattr(config, "HIVE_TOKEN", "") or None
    if not token:
        return "HiveOS не подключён (нет HIVE_TOKEN).", keyboards.back_to("home")
    try:
        farms = await hive.fetch_farms(token)
    except Exception as e:
        return f"Не удалось получить фермы: {e}", keyboards.back_to("home")
    watched = {r["farm_id"]: r for r in await db.list_hive_farms(tg_id)}
    text = ("⛏ <b>Фермы HiveOS</b>\n"
            "Отметь фермы под наблюдение. ⚡ — задать цену электричества ($/кВт⋅ч) "
            "для расчёта прибыли.")
    return text, keyboards.hive_farms_screen(farms, watched)


@dp.callback_query(F.data == "farms")
async def cb_farms(cq: CallbackQuery):
    await cq.answer()
    text, markup = await build_farms_view(cq.from_user.id)
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("hfarm:"))
async def cb_hfarm(cq: CallbackQuery):
    fid = int(cq.data.split(":")[1])
    token = getattr(config, "HIVE_TOKEN", "") or None
    name = str(fid)
    if token:
        try:
            farms = await hive.fetch_farms(token)
            name = next((f["name"] for f in farms if f["id"] == fid), str(fid))
        except Exception:
            pass
    on = await db.toggle_hive_farm(cq.from_user.id, fid, name)
    await cq.answer("Под наблюдением" if on else "Убрано")
    text, markup = await build_farms_view(cq.from_user.id)
    await _safe_edit(cq, text, markup)


@dp.callback_query(F.data.startswith("hkwh:"))
async def cb_hkwh(cq: CallbackQuery, state: FSMContext):
    fid = int(cq.data.split(":")[1])
    await state.set_state(SetKwh.value)
    await state.update_data(farm_id=fid)
    await cq.message.answer("Пришли цену электричества в $/кВт⋅ч (например 0.05). "
                            "0 — убрать цену.")
    await cq.answer()


@dp.message(SetKwh.value)
async def setkwh_value(m: Message, state: FSMContext):
    raw = m.text.strip().replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        await m.answer("Нужно число, например 0.05. Или /start чтобы отменить.")
        return
    data = await state.get_data()
    await state.clear()
    fid = data.get("farm_id")
    await db.set_hive_kwh(m.from_user.id, fid, None if val <= 0 else val)
    await m.answer("Цена сохранена ✅" if val > 0 else "Цена убрана")
    text, markup = await build_farms_view(m.from_user.id)
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


def _accounts_for_coin(rows, coin_key: str):
    out = []
    for r in rows:
        p = registry.get(r["provider_key"]) if "provider_key" in r else None
        if p and p.meta.coin == coin_key:
            out.append(r)
    return out


async def build_earnings_view(tg_id: int, coin_key: str, level: str):
    c = coins.get(coin_key)
    if c is None:
        return "Монета не найдена.", keyboards.back_to("home")

    day_start = _day_start_ts()
    now = time.time()
    usd = await prices.get_price(c)

    # доход за сегодня (с пул-стороны) + пометка неполного дня
    rows = await db.list_accounts(tg_id)
    mined_today, first_ts, partial = 0.0, None, False
    for a in _accounts_for_coin(rows, coin_key):
        latest = await db.latest_snapshot(tg_id, a["id"])
        first = await db.first_snapshot_today(tg_id, a["id"], day_start)
        if latest and first and latest["mined"] >= first["mined"]:
            mined_today += latest["mined"] - first["mined"]
            ft = first["captured_at"]
            first_ts = ft if first_ts is None else min(first_ts, ft)
            if ft > day_start + 1800:   # первый срез позже чем +30 мин от полуночи
                partial = True

    hours = ((now - first_ts) / 3600.0) if first_ts else 0.0

    # риг-сторона
    token = getattr(config, "HIVE_TOKEN", "") or None
    workers, kwh_by_farm = [], {}
    if token:
        farm_ids = await db.list_hive_farm_ids(tg_id)
        if farm_ids:
            try:
                workers = await hive.farms_workers(token, farm_ids)
            except Exception:
                workers = []
            kwh_by_farm = {r["farm_id"]: r["kwh_usd"] for r in await db.list_hive_farms(tg_id)}

    res = earnings.compute(c, mined_today, usd, workers, kwh_by_farm, hours)
    rowsL = res["levels"].get(level, [])

    title = {"model": "по моделям", "card": "по картам", "rig": "по ригам"}.get(level, level)
    lines = [f"💰 <b>{c.name}</b> — доходность {title}"]
    rev = res["revenue_usd"]
    lines.append(f"Выручка за сегодня: {('$%.2f' % rev) if rev is not None else '— (нет курса)'}")
    lines.append(f"Намайнено за сегодня: {mined_today:.2f} ({hours:.1f} ч)")
    if partial:
        lines.append("⚠️ неполный день: учёт с первого среза, не с полуночи")
    lines.append("")

    if not rowsL:
        lines.append("Нет данных по ригам этой монеты (отметь ферму в ⛏ Фермы).")
    else:
        for r in rowsL:
            cnt = f" ×{r['count']}" if r["count"] > 1 else ""
            rv = f"${r['revenue']:.2f}" if r["revenue"] is not None else "—"
            pr = f"${r['profit']:.2f}" if r["profit"] is not None else "—"
            lines.append(f"<b>{r['name']}</b>{cnt}")
            lines.append(f"   выручка {rv} · э/э {r['kwh']:.1f} кВт⋅ч ({r['power']:.0f} Вт) · прибыль {pr}")
    if any(r["profit"] is None for r in rowsL):
        lines.append("\n<i>прибыль «—» — задай цену кВт⋅ч в ⛏ Фермы</i>")

    return "\n".join(lines), keyboards.earnings_screen(coin_key, level)


@dp.callback_query(F.data.startswith("earn:"))
async def cb_earn(cq: CallbackQuery):
    _, coin_key, level = cq.data.split(":", 2)
    await cq.answer("Считаю…")
    text, markup = await build_earnings_view(cq.from_user.id, coin_key, level)
    await _safe_edit(cq, text, markup)


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

    # слежение за версиями репозиториев
    vmin = getattr(config, "VERSION_CHECK_MINUTES", 60)
    scheduler.add_job(
        watcher.check_all, "interval",
        minutes=vmin, args=[bot],
        max_instances=1, coalesce=True,
    )
    # первая фиксация версий вскоре после старта (молча запомнит текущие)
    scheduler.add_job(watcher.check_all, "date", args=[bot])
    scheduler.start()

    log.info("bot + collector запущены (опрос %d мин, проверка версий %d мин)",
             config.POLL_MINUTES, vmin)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await collector.shutdown()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())