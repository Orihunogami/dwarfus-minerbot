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
