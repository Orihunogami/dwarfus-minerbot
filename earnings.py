"""
Расчёт доходности по картам/моделям/ригам.

Вход:
  coin          — coins.Coin (нужен hive_symbol для фильтра карт по монете),
  mined_today   — намайнено монет с начала дня (с пул-стороны),
  price_usd     — курс монеты ($), или None,
  workers       — [{worker, farm_id, gpus:[{model,coin,hash,power,bus}]}] (Hive),
  kwh_by_farm   — {farm_id: $/кВт⋅ч | None},
  hours         — часов с первого среза за сегодня (для потребления).

Логика:
  выручка монеты = mined_today * price; делится по картам пропорц. их хешу.
  потребление карты = power * hours / 1000 (кВт⋅ч); цена -> стоимость -> прибыль.
  Нет цены курса -> выручки нет (None). Нет цены кВт⋅ч у фермы -> нет стоимости.
"""
from __future__ import annotations


def compute(coin, mined_today: float, price_usd: float | None,
            workers: list[dict], kwh_by_farm: dict, hours: float) -> dict:
    sym = (coin.hive_symbol or "").upper()

    items, total_hash = [], 0.0
    for w in workers:
        for g in w.get("gpus", []):
            if sym and (g.get("coin") or "").upper() != sym:
                continue
            items.append({"model": g["model"], "hash": g["hash"], "power": g["power"],
                          "worker": w["worker"], "farm_id": w["farm_id"], "bus": g["bus"]})
            total_hash += g["hash"]

    revenue_usd = (mined_today * price_usd) if price_usd is not None else None

    per = []
    for it in items:
        share = it["hash"] / total_hash if total_hash else 0.0
        rev = revenue_usd * share if revenue_usd is not None else None
        kwh = it["power"] * hours / 1000.0
        kp = kwh_by_farm.get(it["farm_id"])
        cost = kwh * kp if kp is not None else None
        profit = (rev - cost) if (rev is not None and cost is not None) else None
        per.append({**it, "rev": rev, "kwh": kwh, "cost": cost, "profit": profit})

    def group(keyfn, namefn) -> list[dict]:
        acc: dict = {}
        for p in per:
            k = keyfn(p)
            d = acc.setdefault(k, {"name": namefn(p), "count": 0, "power": 0.0, "kwh": 0.0,
                                   "rev": 0.0, "cost": 0.0, "rev_ok": True, "cost_ok": True})
            d["count"] += 1
            d["power"] += p["power"]
            d["kwh"] += p["kwh"]
            if p["rev"] is None:
                d["rev_ok"] = False
            else:
                d["rev"] += p["rev"]
            if p["cost"] is None:
                d["cost_ok"] = False
            else:
                d["cost"] += p["cost"]
        out = []
        for d in acc.values():
            rev = d["rev"] if d["rev_ok"] else None
            cost = d["cost"] if d["cost_ok"] else None
            profit = (rev - cost) if (rev is not None and cost is not None) else None
            out.append({"name": d["name"], "count": d["count"], "power": d["power"],
                        "kwh": d["kwh"], "revenue": rev, "cost": cost, "profit": profit})
        return out

    levels = {
        "model": group(lambda p: p["model"], lambda p: p["model"]),
        "rig": group(lambda p: p["worker"], lambda p: p["worker"]),
        "card": group(lambda p: (p["worker"], p["bus"]),
                      lambda p: f'{p["model"]} · {p["worker"]}#{p["bus"]}'),
    }
    return {"revenue_usd": revenue_usd, "total_hash": total_hash,
            "gpus": len(items), "hours": hours, "levels": levels}
