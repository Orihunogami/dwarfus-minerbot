"""
Общий рендер карточки статистики — без привязки к конкретной монете.

Балансы печатаются всегда; шапка (хешрейты/KPI) собирается из snap.headline
в единицах meta.rate_unit; воркеры — из snap.workers. Единица "p/s" больше
нигде не захардкожена, поэтому другая монета с MH/s ляжет без правок рендера.
"""
from __future__ import annotations

from providers.base import ProviderMeta, Snapshot


def render_card(meta: ProviderMeta, snap: Snapshot, *, label: str | None,
                ident: str, income_today: float | None, ago: str) -> str:
    name = label or (ident[:10] + "…")
    b = snap.balances
    inc = f"+{income_today:.2f}" if income_today is not None else "копим данные"
    overview = (
        f"Намайнено        {b.mined:>10.2f}\n"
        f"Заблокировано    {b.locked:>10.2f}\n"
        f"Доступно         {b.transferable:>10.2f}\n"
        f"Прогноз сегодня  {b.today_est:>10.2f}\n"
        f"Доход за сутки   {inc:>10}"
    )

    if snap.headline:
        parts = " · ".join(f"{m.label or m.key} {m.value:.0f}" for m in snap.headline)
        head = f"⚡️ Хешрейт: {parts} {meta.rate_unit}\n"
    else:
        head = ""

    if snap.workers:
        lines = "\n".join(
            f" • {w.name:<11} {w.rate:>6.0f} {meta.rate_unit}" for w in snap.workers
        )
        dev_block = f"🖥 Онлайн ({len(snap.workers)}):\n{lines}\n"
    else:
        dev_block = ""

    return (
        f"💎 <b>{name}</b>\n"
        f"<pre>{overview}</pre>"
        f"{head}{dev_block}"
        f"🕒 обновлено {ago}"
    )
