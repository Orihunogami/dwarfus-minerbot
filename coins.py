"""
Карточки монет — конфиг (правится руками).

Монета держит метаданные:
  repos  — какие репозитории с ней связаны (софт, за которым ПОЗЖЕ будем
           следить на новые версии; пока просто хранится и показывается),
  price  — откуда брать курс (для денежной сводки; выборку прикрутим, когда
           дойдём до денег — сейчас это просто адрес источника).

Логики майнинга тут НЕТ — она в providers/. Монета = группировка для меню
плюс эти привязки.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Repo:
    """Связанный с монетой репозиторий. kind — что за софт; watch — как ловить новое."""
    url: str
    kind: str = "miner"            # miner / wallet / node ...
    watch: str = "release"         # release / tag / commit (пригодится для слоя уведомлений)


@dataclass(frozen=True)
class PriceSource:
    """Откуда брать курс. Сейчас — только адрес источника, без живой выборки."""
    kind: str                      # "coinmarketcap" / "coingecko" / ...
    symbol: str                    # тикер/slug на источнике
    url: str | None = None         # человеку — куда сходить глазами


@dataclass(frozen=True)
class Coin:
    key: str                       # совпадает с ProviderMeta.coin
    name: str                      # человеку, напр. "Nockchain"
    repos: tuple[Repo, ...] = ()
    price: PriceSource | None = None


COINS: dict[str, Coin] = {
    "nockchain": Coin(
        key="nockchain",
        name="Nockchain",
        repos=(
            Repo(
                url="https://github.com/GoldenMinerNetwork/nockchain-wallet",
                kind="wallet",
                watch="release",
            ),
        ),
        price=PriceSource(
            kind="coinmarketcap",
            symbol="nockchain",
            url="https://coinmarketcap.com/currencies/nockchain/",
        ),
    ),
}


def get(coin_key: str) -> Coin | None:
    return COINS.get(coin_key)
