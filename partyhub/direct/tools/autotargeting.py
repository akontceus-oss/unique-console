"""Автотаргетинг: какие запросы он приводит, сколько на них уходит и что из них стоит минусовать или забрать в ключи."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select

from ...config import load_rules
from ...models import SearchQuery
from ...rules.engine import Proposal, _money
from ...rules.state import clean_word, current_negatives
from ..common import div
from ..registry import Col, DirectTool, ToolContext, ToolResult, register

CATEGORY_RU = {"EXACT": "Целевые", "NARROW": "Узкие", "ALTERNATIVE": "Альтернативные", "ACCESSORY": "Сопутствующие",
               "BROADER": "Широкие", "COMPETITOR": "Конкурентные"}


@register
class Autotargeting(DirectTool):
    id = "autotargeting"
    title = "Автотаргетинг"
    group = "Чистка"
    order = 30
    description = (
        "Запросы, которые привёл автотаргетинг, с расходом и конверсиями. Дорогие без конверсий предлагаются в минус "
        "целой фразой, конвертирующие дешевле цели — кандидаты в ключевые фразы. В сводке — доля автотаргетинга в "
        "расходе поиска и разбивка по категориям (целевые, широкие, конкурентные…)."
    )
    campaign_types = ("autotargeting", "search", "unified")
    columns = [
        Col("status", "Статус", "chip"),
        Col("query", "Запрос"),
        Col("category", "Категория", wide=True),
        Col("campaign", "Кампания", wide=True),
        Col("clicks", "Клики", "int"),
        Col("cost", "Расход", "money"),
        Col("conversions", "Конверсии", "int"),
        Col("cpa", "Цена конверсии", "money"),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        cfg = self.config()
        target_cpl = load_rules()["economics"]["target_cpl"]
        q = pd.read_sql(select(SearchQuery).where(SearchQuery.date.between(ctx.d1, ctx.d2)), ctx.session.connection())
        if q.empty or not (q["criterion_type"] == "AUTOTARGETING").any():
            return ToolResult(notes=["Нет запросов автотаргетинга за период. Либо он выключен, либо Директ не вернул "
                                     "разметку CriterionType — тогда проверьте поля отчёта в песочнице."])
        q["cost"] = q["cost"].astype(float)
        has_conv = q["conversions"].notna().any()
        q["conversions"] = q["conversions"].fillna(0).astype(int)
        names = q.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
        auto = q[q["criterion_type"] == "AUTOTARGETING"]
        negatives = current_negatives(ctx.session)
        mult = float(cfg.get("cost_multiplier", 1.0))
        min_clicks = int(cfg.get("min_clicks", 3))
        max_words = int(cfg.get("max_words", 7))

        rows, proposals = [], []
        for (cid, query), g in auto.groupby(["campaign_id", "query"]):
            cid = int(cid)
            cost, clicks, conv = float(g["cost"].sum()), int(g["clicks"].sum()), int(g["conversions"].sum())
            cat = g["targeting_category"].dropna()
            cpa = div(cost, conv)
            if clean_word(query) in negatives.get(cid, set()):
                continue
            if has_conv and conv == 0 and clicks >= min_clicks and cost >= mult * target_cpl and len(query.split()) <= max_words:
                status = ["В минус", "bad"]
                proposals.append(Proposal(
                    "direct_autotargeting", cfg.get("level", "approval"), "add_negative", query,
                    f"Автотаргетинг привёл запрос «{query}»: {clicks} кликов, {_money(cost)}, конверсий нет.",
                    [cid], [str(names.get(cid, cid))], {"cost": round(cost, 2), "clicks": clicks},
                ))
            elif conv and cpa is not None and cpa <= target_cpl:
                status = ["В ключи", "good"]
            else:
                status = ["Наблюдаем", "muted"]
            rows.append({"status": status, "query": query, "category": CATEGORY_RU.get(cat.iloc[-1], cat.iloc[-1]) if len(cat) else "—",
                         "campaign": names.get(cid, cid), "campaign_id": cid, "clicks": clicks, "cost": round(cost, 2),
                         "conversions": conv if has_conv else None, "cpa": cpa})
        rows.sort(key=lambda r: -r["cost"])

        search_cost = float(q["cost"].sum())
        auto_cost = float(auto["cost"].sum())
        notes = []
        if auto["targeting_category"].notna().any():
            by_cat = auto.groupby("targeting_category").agg(cost=("cost", "sum"), conv=("conversions", "sum"))
            notes.append("По категориям: " + "; ".join(
                f"{CATEGORY_RU.get(k, k)} — {_money(v.cost)}" + (f", конверсий {int(v.conv)}" if has_conv else "")
                for k, v in by_cat.sort_values("cost", ascending=False).iterrows()))
        if not has_conv:
            notes.append("Конверсий по запросам нет: задайте DIRECT_GOAL_IDS — без них в минус ничего не предлагается.")
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Доля автотаргетинга в расходе", "value": div(auto_cost, search_cost), "fmt": "pct"},
            {"label": "Расход автотаргетинга", "value": round(auto_cost, 2), "fmt": "money"},
            {"label": "Запросов в минус", "value": len(proposals), "fmt": "int", "tone": "warn" if proposals else ""},
            {"label": "Кандидатов в ключи", "value": sum(1 for r in rows if r["status"][0] == "В ключи"), "fmt": "int",
             "tone": "good"},
        ])
