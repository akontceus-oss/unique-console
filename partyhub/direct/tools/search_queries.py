"""Чистка поисковых запросов: слова, на которые уходят деньги без конверсий, и стоп-слова."""
from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd
from sqlalchemy import select

from ...config import load_rules
from ...models import SearchQuery
from ...rules.engine import Proposal, _money, negative_keyword_proposals, word_pattern
from ...rules.state import clean_word, current_negatives
from ..registry import Col, DirectTool, ToolContext, ToolResult, register

WORD_RE = re.compile(r"[\w-]+", re.U)
# Служебные слова: их нельзя минусовать отдельно.
SERVICE_WORDS = {"для", "на", "в", "во", "с", "со", "и", "или", "по", "от", "до", "из", "как", "где", "что", "это",
                 "у", "к", "о", "об", "за", "без", "под", "над", "при", "the", "a"}


def words_of(query: str) -> set[str]:
    return {w for w in WORD_RE.findall(str(query).lower()) if len(w) >= 3 and w not in SERVICE_WORDS and not w.isdigit()}


@register
class QueryCleaning(DirectTool):
    id = "search_queries"
    title = "Чистка запросов"
    group = "Чистка"
    order = 20
    description = (
        "Разбирает поисковые запросы на слова и считает по каждому расход, клики и конверсии. Предлагает в минус "
        "стоп-слова из rules.yaml и слова, которые потратили больше заданной доли целевой цены заявки без конверсий. "
        "Слова из ваших ключевых фраз не предлагаются никогда. Конверсии нужны цели в DIRECT_GOAL_IDS."
    )
    campaign_types = ("search", "autotargeting", "unified")
    columns = [
        Col("status", "Статус", "chip"),
        Col("word", "Слово"),
        Col("queries", "Запросов", "int", wide=True),
        Col("clicks", "Клики", "int"),
        Col("cost", "Расход", "money"),
        Col("conversions", "Конверсии", "int"),
        Col("examples", "Примеры запросов", "long", wide=True),
        Col("campaigns", "Кампании", "text", wide=True),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        cfg = self.config()
        rules = load_rules()
        target_cpl = rules["economics"]["target_cpl"]
        q = pd.read_sql(select(SearchQuery).where(SearchQuery.date.between(ctx.d1, ctx.d2)), ctx.session.connection())
        if q.empty:
            return ToolResult(notes=["Нет поисковых запросов за период: соберите данные Директа."])
        q["cost"] = q["cost"].astype(float)
        has_conv = q["conversions"].notna().any()
        q["conversions"] = q["conversions"].fillna(0).astype(int)
        names = q.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
        negatives = current_negatives(ctx.session)
        keyword_words: set[str] = set()
        for crit in q["criterion"].dropna().unique():
            keyword_words |= {clean_word(w) for w in words_of(crit)}
        keyword_words |= {clean_word(w) for w in cfg.get("protected_words", [])}
        stop = {clean_word(w): w for w in rules["rules"].get("negative_keywords", {}).get("stop_words", [])}

        agg: dict[str, dict] = defaultdict(lambda: {"queries": set(), "clicks": 0, "cost": 0.0, "conversions": 0, "cids": set()})
        for r in q.itertuples():
            for w in words_of(r.query):
                a = agg[w]
                a["queries"].add(r.query)
                a["clicks"] += int(r.clicks)
                a["cost"] += float(r.cost)
                a["conversions"] += int(r.conversions)
                a["cids"].add(int(r.campaign_id))

        qcost = q.groupby("query")["cost"].sum().to_dict()
        mult = float(cfg.get("cost_multiplier", 1.0))
        min_clicks = int(cfg.get("min_clicks", 5))
        rows, proposals = [], []
        for w, a in agg.items():
            cids = sorted(c for c in a["cids"] if w not in negatives.get(c, set()))
            if not cids:
                continue
            ex = sorted(a["queries"], key=lambda s: -qcost.get(s, 0))[:3]
            if w in stop or any(word_pattern(sw).search(w) for sw in stop.values()):
                status, tone = "Стоп-слово", "bad"
            elif has_conv and w not in keyword_words and a["conversions"] == 0 and a["clicks"] >= min_clicks \
                    and a["cost"] >= mult * target_cpl:
                status, tone = "Дорого без конверсий", "warn"
                proposals.append(Proposal(
                    "direct_query_words", self.config().get("level", "approval"), "add_negative", w,
                    f"Слово «{w}» в запросах: {a['clicks']} кликов, {_money(a['cost'])}, конверсий нет. Примеры: "
                    + "; ".join(f"«{e}»" for e in ex),
                    cids, [str(names.get(c, c)) for c in cids],
                    {"cost": round(a["cost"], 2), "clicks": a["clicks"], "examples": ex},
                ))
            elif w in keyword_words:
                status, tone = "Из ключевых фраз", "good"
            else:
                status, tone = "Наблюдаем", "muted"
            rows.append({"status": [status, tone], "word": w, "queries": len(a["queries"]), "clicks": a["clicks"],
                         "cost": round(a["cost"], 2), "conversions": a["conversions"] if has_conv else None,
                         "examples": "; ".join(ex), "campaigns": ", ".join(str(names.get(c, c)) for c in cids),
                         "campaign_ids": cids})
        rows.sort(key=lambda r: -r["cost"])
        rows = [r for r in rows if r["status"][0] != "Наблюдаем"] + [r for r in rows if r["status"][0] == "Наблюдаем"][:150]
        proposals = negative_keyword_proposals(ctx.session, ctx.d1, ctx.d2) + proposals
        notes = [] if has_conv else ["Конверсий по запросам нет: задайте DIRECT_GOAL_IDS — без них предлагаются только стоп-слова."]
        junk = sum(r["cost"] for r in rows if r["status"][1] in ("bad", "warn"))
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Запросов", "value": int(q["query"].nunique()), "fmt": "int"},
            {"label": "Слов в минус", "value": len(proposals), "fmt": "int", "tone": "warn" if proposals else ""},
            {"label": "Расход на мусор", "value": round(junk, 2), "fmt": "money", "tone": "bad" if junk else ""},
        ])
