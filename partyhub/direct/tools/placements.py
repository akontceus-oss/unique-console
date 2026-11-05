"""Чистка площадок РСЯ: те же правила, что в утреннем цикле, но по запросу и за выбранный период."""
from __future__ import annotations

from ...pipeline import metrics as M
from ...rules.engine import placement_proposals
from ..registry import Col, DirectTool, ToolContext, ToolResult, register

STATUS = {"bad": "Плохая", "proposed": "Ждёт решения", "watch": "Наблюдаем", "good": "Хорошая", "excluded": "Исключена"}
TONE = {"bad": "bad", "proposed": "warn", "watch": "muted", "good": "good", "excluded": "muted"}


@register
class PlacementCleaning(DirectTool):
    id = "placements"
    title = "Чистка площадок"
    group = "Чистка"
    order = 10
    description = (
        "Площадки РСЯ без заявок дороже 1,5 целевой цены заявки, приложения со случайными кликами и площадки "
        "с отказами выше 70%. Пороги — в config/rules.yaml (rules). Площадки с бронями не трогаются."
    )
    campaign_types = ("network", "unified")
    columns = [
        Col("status", "Статус", "chip"),
        Col("placement", "Площадка"),
        Col("cost", "Расход", "money"),
        Col("clicks", "Клики", "int"),
        Col("ctr", "CTR", "pct", wide=True),
        Col("bounce_rate", "Отказы", "pct"),
        Col("leads", "Заявки", "int"),
        Col("bookings", "Брони", "int", wide=True),
        Col("cpl", "Цена заявки", "money"),
        Col("reason", "Почему", "long", wide=True),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        proposals = placement_proposals(ctx.session, ctx.d1, ctx.d2)
        why = {p.target: p.reason for p in proposals}
        rows = []
        for r in M.placements(ctx.session, ctx.d1, ctx.d2):
            st = "proposed" if r["placement"] in why and r["status"] not in ("excluded", "proposed") else r["status"]
            rows.append({**r, "status": [STATUS.get(st, st), TONE.get(st, "muted")], "status_code": st,
                         "reason": why.get(r["placement"], ""), "campaigns": ", ".join(r["campaigns"])})
        bad_cost = sum(r["cost"] for r in rows if r["status_code"] in ("bad", "proposed"))
        return ToolResult(
            rows=rows,
            proposals=proposals,
            summary=[
                {"label": "Площадок", "value": len(rows), "fmt": "int"},
                {"label": "К исключению", "value": len(proposals), "fmt": "int", "tone": "warn" if proposals else ""},
                {"label": "Расход на плохих", "value": round(bad_cost, 2), "fmt": "money", "tone": "bad" if bad_cost else ""},
            ],
        )
