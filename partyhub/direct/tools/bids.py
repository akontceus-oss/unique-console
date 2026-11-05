"""Ставки: целевая цена конверсии в автостратегиях по фактической цене брони или заявки."""
from __future__ import annotations

from ...config import load_rules
from ...rules.engine import Proposal, _money
from ..common import campaign_frame, div, latest_snapshots, week_tag
from ..kinds import strategy_cpa
from ..registry import Col, DirectTool, ToolContext, ToolResult, register

STRATEGY_RU = {
    "AVERAGE_CPA": "Оплата за клики, цена конверсии",
    "PAY_FOR_CONVERSION": "Оплата за конверсии",
    "WB_MAXIMUM_CONVERSION_RATE": "Максимум конверсий",
    "WB_MAXIMUM_CLICKS": "Максимум кликов",
    "AVERAGE_CPC": "Средняя цена клика",
    "HIGHEST_POSITION": "Ручное управление",
    "MANUAL_CPM": "Ручное управление",
    "SERVING_OFF": "Показы отключены",
    "NETWORK_DEFAULT": "Как на поиске",
}


def _strategy_name(strategy: dict | None) -> str:
    parts = []
    for side, ru in (("Search", "поиск"), ("Network", "сети")):
        t = ((strategy or {}).get(side) or {}).get("BiddingStrategyType")
        if t and t not in ("SERVING_OFF", "NETWORK_DEFAULT"):
            parts.append(f"{ru}: {STRATEGY_RU.get(t, t)}")
    return "; ".join(parts) or "—"


@register
class BidTargets(DirectTool):
    id = "bids"
    title = "Целевая цена конверсии"
    group = "Ставки"
    order = 50
    description = (
        "Сравнивает фактическую цену брони (или заявки, если броней мало) с целью из rules.yaml и предлагает поправить "
        "целевую цену конверсии в стратегии: дороже цели — снизить, дешевле — поднять, чтобы забрать больше объёма. "
        "Шаг не больше max_step за раз и не чаще раза в неделю. Применяется только после вашего «да»."
    )
    campaign_types = ("search", "network", "unified", "autotargeting")
    columns = [
        Col("status", "Статус", "chip"),
        Col("campaign", "Кампания"),
        Col("strategy", "Стратегия", wide=True),
        Col("basis", "По чему считаем", wide=True),
        Col("actual", "Факт", "money"),
        Col("target", "Цель", "money", wide=True),
        Col("current_cpa", "Целевая цена сейчас", "money"),
        Col("new_cpa", "Предлагаем", "money"),
        Col("change", "Изменение", "pct"),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        cfg = self.config()
        econ = load_rules()["economics"]
        step = float(cfg.get("max_step", 0.2))
        min_change = float(cfg.get("min_change", 0.05))
        min_bookings, min_leads = int(cfg.get("min_bookings", 3)), int(cfg.get("min_leads", 5))
        frame = campaign_frame(ctx.session, ctx.d1, ctx.d2)
        snaps = latest_snapshots(ctx.session)
        tag = week_tag(ctx.d2)
        rows, proposals = [], []
        for r in frame.sort_values("cost", ascending=False).itertuples():
            cid = int(r.campaign_id)
            strategy = (snaps.get(cid) or {}).get("BiddingStrategy")
            cpa = strategy_cpa(strategy)
            if r.bookings >= min_bookings:
                basis, actual, target = "цена брони", r.cost / r.bookings, econ["target_cpb"]
            elif r.leads >= min_leads:
                basis, actual, target = "цена заявки", r.cost / r.leads, econ["target_cpl"]
            else:
                basis, actual, target = "мало данных", None, None
            row = {"campaign": r.campaign, "campaign_id": cid, "strategy": _strategy_name(strategy), "basis": basis,
                   "actual": round(actual, 2) if actual else None, "target": target,
                   "current_cpa": round(next(iter(cpa.values())), 2) if cpa else None, "new_cpa": None, "change": None}
            if actual is None:
                row["status"] = ["Мало данных", "muted"]
            elif not cpa:
                ok = actual <= target
                row["status"] = ["Нет целевой цены", "muted"]
                row["basis"] += (" — в цели, можно перейти на стратегию с оплатой за конверсии" if ok
                                 else " — дороже цели, ставки в этой стратегии правятся вручную")
            else:
                ratio = max(1 - step, min(1 + step, target / actual))
                if abs(ratio - 1) < min_change:
                    row["status"] = ["В цели", "good"]
                else:
                    new = {side: float(round(v * ratio)) for side, v in cpa.items()}
                    row.update(new_cpa=next(iter(new.values())), change=round(ratio - 1, 4),
                               status=["Снизить", "warn"] if ratio < 1 else ["Поднять", "good"])
                    proposals.append(Proposal(
                        "direct_bids", "approval", "set_cpa", f"Целевая цена · {tag}",
                        f"{basis.capitalize()} {_money(actual)} при цели {_money(target)}: целевая цена конверсии "
                        f"{_money(next(iter(cpa.values())))} → {_money(next(iter(new.values())))} ({(ratio - 1) * 100:+.0f}%).",
                        [cid], [r.campaign],
                        {"new_cpa": new, "old_cpa": cpa, "basis": basis, "actual": round(actual, 2), "target": target},
                    ))
            rows.append(row)
        notes = []
        if not any(r["current_cpa"] for r in rows):
            notes.append("Ни в одной кампании нет стратегии с целевой ценой конверсии (или ещё нет снимка настроек) — "
                         "инструмент только показывает факт против цели.")
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Кампаний", "value": len(rows), "fmt": "int"},
            {"label": "Снизить цену", "value": sum(1 for r in rows if r["status"][0] == "Снизить"), "fmt": "int", "tone": "warn"},
            {"label": "Поднять цену", "value": sum(1 for r in rows if r["status"][0] == "Поднять"), "fmt": "int", "tone": "good"},
        ])
