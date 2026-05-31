"""
Оффлайн-проверка плагинной модели — без сети и без кредов.

Берём фейковый ответ пула, прогоняем через нормализацию и реестр, печатаем:
реестр провайдеров, меню (монета -> способы), карточку монеты из конфига,
форму входа и итоговую карточку статистики.

Запуск:  python selftest.py
"""
from goldenminer import Snapshot as GMSnap
from providers.goldenminer import to_snapshot
from providers import registry
import coins
import render


def fake_gm() -> GMSnap:
    return GMSnap(
        wallet="nock1qxyabclongwallet",
        captured_at=1717146000,
        mined=12.40, locked=2.00, transferable=3.10, today_est=0.90,
        local_rate=388.0, real_rate=162.0,
        devices_online=2,
        devices=[
            {"name": "rig-1", "local_ip": "10.0.0.5", "rate": 210.0},
            {"name": "rig-2", "local_ip": "10.0.0.6", "rate": 178.0},
        ],
        raw={"info": {}, "power": {}, "devices": {}},
    )


def main() -> None:
    print("== реестр провайдеров ==")
    for p in registry.all_providers():
        print(f"  {p.meta.key} | монета={p.meta.coin} | {p.meta.method} "
              f"| хешрейт {p.meta.rate_unit} "
              f"| выводы={p.capabilities.withdrawals} воркеры={p.capabilities.workers}")

    print("\n== меню: монета -> способы ==")
    for coin_key, provs in registry.by_coin().items():
        c = coins.get(coin_key)
        print(f"  {c.name if c else coin_key}:")
        for p in provs:
            print(f"     - {p.meta.method}")

    print("\n== карточка монеты Nockchain (из конфига) ==")
    c = coins.get("nockchain")
    print(f"  курс: {c.price.kind} :: {c.price.symbol}  ({c.price.url})")
    for r in c.repos:
        print(f"  репа [{r.kind}/{r.watch}]: {r.url}")

    print("\n== форма входа (бот строит диалог сам) ==")
    gm = registry.get("nockchain.goldenminer")
    for f in gm.auth_schema():
        tail = " (секрет — сообщение удаляется)" if f.secret else ""
        print(f"  поле '{f.key}' -> «{f.label}»{tail}")

    print("\n== нормализация: ответ пула -> единый Snapshot ==")
    snap = to_snapshot(fake_gm())
    print(f"  balances: mined={snap.balances.mined} locked={snap.balances.locked} "
          f"transferable={snap.balances.transferable} today_est={snap.balances.today_est}")
    print("  headline: " + ", ".join(f"{m.label}={m.value:g}{m.unit}" for m in snap.headline))
    print("  workers : " + ", ".join(f"{w.name}@{w.rate:g}" for w in snap.workers))

    print("\n== как карточка выглядит в боте (рендер общий) ==")
    print(render.render_card(gm.meta, snap, label=None, ident="nock1qxyabclongwallet",
                             income_today=0.42, ago="2 мин назад"))


if __name__ == "__main__":
    main()
