"""
Инлайн-клавиатуры бота.

Навигация: дом -> (монета) -> (способ) -> аккаунт -> карточка.
Уровни «монета» и «способ» пока пропускаются (их по одному) — всплывут, когда
монет/способов станет больше. Здесь собраны только те экраны, что нужны сейчас.

callback_data — короткие токены (лимит Telegram 64 байта):
    home              домой
    login             запустить подключение аккаунта
    accounts          управление аккаунтами
    acc:<id>          показать карточку аккаунта (с живым обновлением)
    wd:<id>           выводы по аккаунту
    logout:<id>       удалить аккаунт
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def home_no_accounts() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("➕ Подключить аккаунт", "login")]])


def card_actions(account_id: int, *, with_back: bool, withdrawals: bool,
                 coin_key: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[_btn("🔄 Обновить", f"acc:{account_id}")]]
    second: list[InlineKeyboardButton] = []
    if withdrawals:
        second.append(_btn("💸 Выводы", f"wd:{account_id}"))
    if coin_key:
        second.append(_btn("📦 Монета", f"coin:{coin_key}"))
    rows.append(second)
    rows.append([_btn("⚙️ Аккаунты", "accounts")])
    if with_back:
        rows.append([_btn("⬅️ Назад", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_picker(accounts) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        name = a["label"] or (a["wallet"][:12] + "…")
        flag = "" if a["is_active"] else " ⏸"
        rows.append([_btn(f"💎 {name}{flag}", f"acc:{a['id']}")])
    rows.append([_btn("➕ Подключить ещё", "login")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def accounts_manage(accounts) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        name = a["label"] or (a["wallet"][:12] + "…")
        rows.append([_btn(f"💎 {name}", f"acc:{a['id']}"), _btn("❌", f"logout:{a['id']}")])
    rows.append([_btn("➕ Подключить ещё", "login")])
    rows.append([_btn("⬅️ Назад", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to(data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", data)]])


def hive_farms_screen(farms: list[dict], watched: dict[int, dict]) -> InlineKeyboardMarkup:
    """farms — из API [{id,name,online,total}]; watched — {farm_id: row} выбранные."""
    rows: list[list[InlineKeyboardButton]] = []
    for f in farms:
        fid = f["id"]
        on = fid in watched
        mark = "✅" if on else "☐"
        name = (f["name"] or str(fid))[:20]
        label = f"{mark} {name} ({f['online']}/{f['total']})"
        row = [_btn(label, f"hfarm:{fid}")]
        if on:
            w = watched[fid]
            price = f"{w['kwh_usd']:.3f}$" if w["kwh_usd"] is not None else "⚡цена"
            row.append(_btn(price, f"hkwh:{fid}"))
        rows.append(row)
    rows.append([_btn("⬅️ Назад", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _repo_short(url: str) -> str:
    return url.rstrip("/").split("/")[-1][:18]


def coin_screen(coin_key: str, repos, *, has_suggested: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in repos:
        eye = "👁" if r["watch_enabled"] else "🔕"
        rows.append([
            _btn(f"{eye} {_repo_short(r['url'])}", f"wrepo:{r['id']}"),
            _btn("❌", f"rmrepo:{r['id']}"),
        ])
    if has_suggested and not repos:
        rows.append([_btn("➕ Добавить рекомендованную", f"seedrepo:{coin_key}")])
    rows.append([_btn("➕ Добавить репу", f"addrepo:{coin_key}")])
    if repos:
        rows.append([_btn("🔍 Проверить сейчас", f"chkcoin:{coin_key}")])
    rows.append([_btn("💰 Доходность", f"earn:{coin_key}:model")])
    rows.append([_btn("⬅️ Назад", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def earnings_screen(coin_key: str, level: str) -> InlineKeyboardMarkup:
    def lbl(lv: str, title: str) -> InlineKeyboardButton:
        return _btn(("• " if lv == level else "") + title, f"earn:{coin_key}:{lv}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [lbl("model", "Модели"), lbl("card", "Карты"), lbl("rig", "Риги")],
        [_btn("⬅️ Назад", f"coin:{coin_key}")],
    ])
