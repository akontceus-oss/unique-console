"""Конверсии кампаний: клик → заявка → бронь, цена заявки и брони против цели, просадки и проблемы с разметкой."""
from __future__ import annotations

from ...config import load_rules
from ...pipeline import metrics as M
from ...rules.engine import Proposal, _money, _pct
from ..common import campaign_frame, campaign_types, div, prev_period, week_tag
from ..registry import CAMPAIGN_TYPES, Col, DirectTool, ToolContext, ToolResult, register


@register
class Conversions(DirectTool):
    id = "conversions"
    title = "Конверсии кампаний"
    group = "Конверсии"
    order = 40
    description = (
        "Для каждой кампании: конверсия клика в заявку и заявки в бронь, цена заявки и брони против цели и изменение "
        "к прошлому периоду. Заявки и брони — из CRM, а не из целей Метрики. Сигналит, если кампания тратит без заявок, "
        "конверсия резко упала или заявки из Директа приходят без меток кампании."
    )
    columns = [
        Col("status", "Статус", "chip"),
        Col("campaign", "Кампания"),
        Col("types", "Тип", wide=True),
        Col("cost", "Расход", "money"),
        Col("clicks", "Клики", "int", wide=True),
        Col("leads", "Заявки", "int"),
        Col("bookings", "Брони", "int"),
        Col("cr", "Клик → заявка", "pct"),
        Col("cr_prev", "Было", "pct", wide=True),
        Col("lead_to_booking", "Заявка → бронь", "pct", wide=True),
        Col("cpl", "Цена заявки", "money"),
        Col("cpb", "Цена брони", "money"),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        cfg = self.config()
        econ = load_rules()["economics"]
        cur = campaign_frame(ctx.session, ctx.d1, ctx.d2)
        if cur.empty:
            return ToolResult(notes=["Нет статистики Директа за период."])
        p1, p2 = prev_period(ctx.d1, ctx.d2)
        prev = campaign_frame(ctx.session, p1, p2).set_index("campaign_id")
        types = campaign_types(ctx.session, ctx.d1, ctx.d2)
        drop = float(cfg.get("cr_drop", 0.4))
        min_clicks = int(cfg.get("min_clicks", 100))
        no_leads_mult = float(cfg.get("no_leads_cost_multiplier", 2.0))
        tag = week_tag(ctx.d2)

        rows, proposals = [], []
        for r in cur.sort_values("cost", ascending=False).itertuples():
            cid = int(r.campaign_id)
            cr = div(r.leads, r.clicks)
            pv = prev.loc[cid] if cid in prev.index else None
            cr_prev = div(pv["leads"], pv["clicks"]) if pv is not None else None
            cpl, cpb = div(r.cost, r.leads), div(r.cost, r.bookings)
            if r.leads == 0 and r.cost >= no_leads_mult * econ["target_cpl"]:
                status = ["Нет заявок", "bad"]
                proposals.append(Proposal(
                    "direct_no_leads", "alert", "alert", f"{r.campaign} · без заявок · {tag}",
                    f"Кампания потратила {_money(r.cost)} без единой заявки. Проверьте метки в ссылках, цель и посадочную.",
                    [cid], [r.campaign], {"cost": round(float(r.cost), 2), "clicks": int(r.clicks)},
                ))
            elif cr is not None and cr_prev and r.clicks >= min_clicks and cr <= cr_prev * (1 - drop):
                status = ["Просадка", "bad"]
                proposals.append(Proposal(
                    "direct_cr_drop", "alert", "alert", f"{r.campaign} · конверсия · {tag}",
                    f"Конверсия клика в заявку упала с {_pct(cr_prev)} до {_pct(cr)} при {int(r.clicks)} кликах.",
                    [cid], [r.campaign], {"cr": cr, "cr_prev": cr_prev, "clicks": int(r.clicks)},
                ))
            elif cpb is not None and cpb <= econ["target_cpb"]:
                status = ["В цели", "good"]
            elif cpb is not None or (cpl is not None and cpl > econ["target_cpl"]):
                status = ["Дорого", "warn"]
            else:
                status = ["Мало данных", "muted"]
            rows.append({
                "status": status, "campaign": r.campaign, "campaign_id": cid,
                "types": ", ".join(CAMPAIGN_TYPES[t] for t in types.get(cid, [])),
                "cost": round(float(r.cost), 2), "clicks": int(r.clicks), "leads": int(r.leads), "bookings": int(r.bookings),
                "cr": cr, "cr_prev": cr_prev, "lead_to_booking": div(r.bookings, r.leads), "cpl": cpl, "cpb": cpb,
            })

        notes = []
        leads = M._leads(ctx.session, ctx.d1, ctx.d2)
        if not leads.empty:
            dl = leads[leads["is_direct"] & ~leads["is_spam"]]
            lost = int(dl["campaign_id"].isna().sum())
            if len(dl) and lost / len(dl) >= float(cfg.get("untagged_share", 0.1)):
                notes.append(f"{lost} из {len(dl)} заявок из Директа пришли без номера кампании — проверьте utm_campaign={{campaign_id}} "
                             "в ссылках и в Callibri.")
        cost, n_leads, n_book = float(cur["cost"].sum()), int(cur["leads"].sum()), int(cur["bookings"].sum())
        cpb = div(cost, n_book)
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Клик → заявка", "value": div(n_leads, int(cur["clicks"].sum())), "fmt": "pct"},
            {"label": "Заявка → бронь", "value": div(n_book, n_leads), "fmt": "pct"},
            {"label": "Цена заявки", "value": div(cost, n_leads), "fmt": "money"},
            {"label": "Цена брони", "value": cpb, "fmt": "money", "tone": "good" if cpb and cpb <= econ["target_cpb"] else "warn"},
            {"label": "Сигналов", "value": len(proposals), "fmt": "int", "tone": "bad" if proposals else ""},
        ])
