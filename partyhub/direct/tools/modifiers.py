"""Корректировки ставок по устройствам, полу и возрасту — по цене конверсии в каждой группе."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select

from ...config import load_rules
from ...models import DirectSliceStat
from ...rules.engine import Proposal, _money
from ..common import div, week_tag
from ..kinds import MODIFIER_RANGE, slice_label
from ..registry import Col, DirectTool, ToolContext, ToolResult, register

SLICE_RU = {"device": "Устройство", "demography": "Пол и возраст"}


@register
class BidModifiers(DirectTool):
    id = "modifiers"
    title = "Корректировки ставок"
    group = "Ставки"
    order = 60
    description = (
        "Цена конверсии по устройствам и по полу × возрасту против средней по кампании. Группа дешевле средней — "
        "повышающая корректировка, дороже или без конверсий при заметном расходе — понижающая. Не больше ±30% за раз "
        "(max_step), шаг округляется до 5%. Конверсии — по целям DIRECT_GOAL_IDS. Применяется после вашего «да»."
    )
    campaign_types = ("search", "network", "unified", "autotargeting")
    columns = [
        Col("status", "Статус", "chip"),
        Col("campaign", "Кампания"),
        Col("slice", "Срез", wide=True),
        Col("group", "Группа"),
        Col("clicks", "Клики", "int", wide=True),
        Col("cost", "Расход", "money"),
        Col("conversions", "Конверсии", "int"),
        Col("cpa", "Цена конверсии", "money"),
        Col("campaign_cpa", "Средняя по кампании", "money", wide=True),
        Col("modifier", "Корректировка", "text"),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        cfg = self.config()
        target_cpl = load_rules()["economics"]["target_cpl"]
        min_conv = int(cfg.get("min_conversions", 5))
        min_diff = int(cfg.get("min_diff", 10))
        step = int(cfg.get("max_step", 30))
        lo = max(MODIFIER_RANGE[0], int(cfg.get("min_modifier", 50)), 100 - step)
        hi = min(MODIFIER_RANGE[1], int(cfg.get("max_modifier", 150)), 100 + step)
        df = pd.read_sql(select(DirectSliceStat).where(DirectSliceStat.date.between(ctx.d1, ctx.d2)), ctx.session.connection())
        if df.empty:
            return ToolResult(notes=["Нет срезов по устройствам и демографии: они собираются вместе со статистикой Директа."])
        df["cost"] = df["cost"].astype(float)
        names = df.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
        tag = week_tag(ctx.d2)
        rows, proposals = [], []
        for (cid, sl), g in df.groupby(["campaign_id", "slice"]):
            cid = int(cid)
            total_cost, total_conv = float(g["cost"].sum()), int(g["conversions"].sum())
            camp_cpa = div(total_cost, total_conv)
            for value, v in g.groupby("value"):
                if "UNKNOWN" in value:
                    continue
                cost, clicks, conv = float(v["cost"].sum()), int(v["clicks"].sum()), int(v["conversions"].sum())
                cpa = div(cost, conv)
                mod = None
                if camp_cpa and conv >= min_conv:
                    mod = int(round(max(lo, min(hi, 100 * camp_cpa / cpa)) / 5) * 5)
                elif conv == 0 and cost >= 2 * (camp_cpa or target_cpl):
                    mod = lo
                row = {"campaign": names.get(cid, cid), "campaign_id": cid, "slice": SLICE_RU.get(sl, sl),
                       "group": slice_label(sl, value), "clicks": clicks, "cost": round(cost, 2),
                       "conversions": conv, "cpa": cpa, "campaign_cpa": camp_cpa, "modifier": "—"}
                if mod is None:
                    row["status"] = ["Мало данных", "muted"]
                elif abs(mod - 100) < min_diff:
                    row["status"] = ["Без корректировки", "good"]
                else:
                    row["modifier"] = f"{mod - 100:+d}%"
                    row["status"] = ["Понизить", "warn"] if mod < 100 else ["Повысить", "good"]
                    why = (f"конверсий нет при расходе {_money(cost)}" if conv == 0
                           else f"цена конверсии {_money(cpa)} против {_money(camp_cpa)} в среднем по кампании")
                    proposals.append(Proposal(
                        "direct_modifiers", "approval", "bid_modifier", f"{slice_label(sl, value)} · {tag}",
                        f"{SLICE_RU.get(sl, sl)} «{slice_label(sl, value)}»: {why}. Корректировка {mod - 100:+d}%.",
                        [cid], [str(names.get(cid, cid))],
                        {"slice": sl, "value": value, "modifier": mod, "cost": round(cost, 2), "conversions": conv},
                    ))
                rows.append(row)
        rows.sort(key=lambda r: -r["cost"])
        notes = [] if df["conversions"].sum() else [
            "В срезах нет конверсий: задайте DIRECT_GOAL_IDS (цели Метрики), иначе корректировки не рассчитать."]
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Групп", "value": len(rows), "fmt": "int"},
            {"label": "Понизить", "value": sum(1 for r in rows if r["status"][0] == "Понизить"), "fmt": "int", "tone": "warn"},
            {"label": "Повысить", "value": sum(1 for r in rows if r["status"][0] == "Повысить"), "fmt": "int", "tone": "good"},
        ])
