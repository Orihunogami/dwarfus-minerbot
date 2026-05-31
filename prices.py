"""
Курсы монет через CoinMarketCap API (ключ админа в env: CMC_API_KEY).

get_price(coin) -> float | None  — USD за 1 монету, с кешем по TTL, чтобы не
жечь кредиты (Basic: 10k/мес). Любая ошибка/отсутствие монеты/нет ключа -> None,
и UI просто не показывает $. Источник берётся из coin.price (см. coins.py);
для kind="coinmarketcap" symbol трактуется как slug.
"""
from __future__ import annotations

import time
import logging

import httpx

import config

log = logging.getLogger("prices")

CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
TTL = 600  # сек: курс кешируется на 10 минут (~4320 запросов/мес на монету)

_cache: dict[str, tuple[float, float | None]] = {}  # key -> (ts, price)


def parse_cmc_quote(payload: dict, convert: str = "USD") -> float | None:
    """Достаёт цену из ответа CMC quotes/latest.
    data может быть {id: {...}} (по slug) или {SYMBOL: [{...}]} (по symbol)."""
    data = payload.get("data") or {}
    for v in data.values():
        item = v[0] if isinstance(v, list) else v
        q = (item.get("quote") or {}).get(convert) or {}
        price = q.get("price")
        if price is not None:
            return float(price)
    return None


async def _fetch_cmc(slug: str, api_key: str, convert: str = "USD") -> float | None:
    headers = {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}
    params = {"slug": slug, "convert": convert}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(CMC_URL, headers=headers, params=params)
        if r.status_code != 200:
            log.warning("CMC %s -> HTTP %s", slug, r.status_code)
            return None
        return parse_cmc_quote(r.json(), convert)


async def get_price(coin) -> float | None:
    """coin — объект coins.Coin (или None). Вернёт USD-цену за монету или None."""
    ps = getattr(coin, "price", None) if coin else None
    if ps is None:
        return None
    key = f"{ps.kind}:{ps.symbol}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]

    price = hit[1] if hit else None  # запасное значение на случай ошибки
    try:
        if ps.kind == "coinmarketcap":
            api_key = getattr(config, "CMC_API_KEY", "") or ""
            if api_key:
                price = await _fetch_cmc(ps.symbol, api_key)
            else:
                log.debug("CMC_API_KEY не задан — курс пропущен")
        else:
            log.warning("неизвестный источник курса: %s", ps.kind)
    except Exception as e:
        log.warning("price fetch error %s: %s", key, e)

    _cache[key] = (now, price)
    return price
