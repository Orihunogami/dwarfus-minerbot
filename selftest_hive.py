"""
Проверка риг-стороны на реальном Hive.

Запуск (токен в env HIVE_TOKEN), id ферм аргументами:
    HIVE_TOKEN=... venv/bin/python selftest_hive.py 2483754 165978
Без аргументов — просто список ферм.
"""
import asyncio
import os
import sys

import hive
import coins


async def main() -> None:
    token = os.environ.get("HIVE_TOKEN", "")
    if not token:
        print("нет HIVE_TOKEN в окружении")
        return

    farms = await hive.fetch_farms(token)
    print("== фермы ==")
    for f in farms:
        print(f"  id={f['id']:<9} online={f['online']:>2}/{f['total']:<3} {f['name']}")

    farm_ids = [int(x) for x in sys.argv[1:]]
    if not farm_ids:
        print("\nукажи id ферм аргументами, чтобы увидеть раскладку по монетам/моделям")
        return

    all_gpus: list[list[dict]] = []
    for fid in farm_ids:
        all_gpus += await hive.fetch_farm_gpus(token, fid)

    agg = hive.aggregate(all_gpus)
    print("\n== раскладка по монетам и моделям карт ==")
    for coin_sym, d in agg.items():
        c = coins.by_hive_symbol(coin_sym)
        cname = c.name if c else coin_sym
        print(f"\n  {coin_sym} ({cname}): хеш={d['hash']:.4f}  мощность={d['power']:.0f} Вт  карт={d['gpus']}")
        for model, m in d["models"].items():
            print(f"     {model:<14} ×{m['count']:<3} хеш={m['hash']:.4f}  {m['power']:.0f} Вт")


if __name__ == "__main__":
    asyncio.run(main())
