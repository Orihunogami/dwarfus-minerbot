"""
Реестр провайдеров: provider_key -> экземпляр Provider.

Добавить монету/способ = импортнуть класс адаптера и дописать одну строчку
в _PROVIDERS. Ядро бота, схема БД и рендер при этом не меняются.
"""
from __future__ import annotations

from .base import Provider
from .goldenminer import GoldenMinerProvider

_PROVIDERS: dict[str, Provider] = {
    p.meta.key: p
    for p in (
        GoldenMinerProvider(),
        # сюда новые: NewCoinSoloProvider(), OtherPoolProvider(), ...
    )
}


def get(provider_key: str) -> Provider | None:
    return _PROVIDERS.get(provider_key)


def all_providers() -> list[Provider]:
    return list(_PROVIDERS.values())


def by_coin() -> dict[str, list[Provider]]:
    """Группировка для меню бота: coin -> [способы этой монеты]."""
    out: dict[str, list[Provider]] = {}
    for p in _PROVIDERS.values():
        out.setdefault(p.meta.coin, []).append(p)
    return out
