"""Общие расчёты для инструментов Директа: типы кампаний и показатели кампаний за период."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CampaignSnapshot, SearchQuery
from ..pipeline import metrics as M


def week_tag(d: date) -> str:
    """«2026-W43» — правки ставок предлагаются не чаще раза в неделю на кампанию и условие."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def latest_snapshots(session: Session) -> dict[int, dict]:
    last = session.scalar(select(CampaignSnapshot.date).order_by(CampaignSnapshot.date.desc()).limit(1))
    if not last:
        return {}
    return {int(s.campaign_id): (s.data or {}) for s in session.scalars(select(CampaignSnapshot).where(CampaignSnapshot.date == last))}


def campaign_types(session: Session, d1: date, d2: date) -> dict[int, list[str]]:
    """{кампания: ["search", "network", "autotargeting", "unified"]} по статистике и настройкам."""
    out: dict[int, set[str]] = {}
    ads = M._ad_stats(session, d1, d2)
    if not ads.empty:
        for cid, g in ads.groupby("campaign_id"):
            nets = set(g["network"])
            out.setdefault(int(cid), set()).update({"search" if n == "SEARCH" else "network" for n in nets})
    auto = session.execute(
        select(SearchQuery.campaign_id).where(SearchQuery.date.between(d1, d2), SearchQuery.criterion_type == "AUTOTARGETING").distinct()
    ).all()
    for (cid,) in auto:
        out.setdefault(int(cid), set()).add("autotargeting")
    for cid, data in latest_snapshots(session).items():
        if data.get("Type") == "UNIFIED_CAMPAIGN":
            out.setdefault(cid, set()).add("unified")
    return {k: sorted(v) for k, v in out.items()}


def campaign_frame(session: Session, d1: date, d2: date) -> pd.DataFrame:
    """Кампании за период: расход, клики, заявки и брони из CRM, выручка."""
    ads = M._ad_stats(session, d1, d2)
    if ads.empty:
        return pd.DataFrame(columns=["campaign_id", "campaign", "cost", "clicks", "impressions", "leads", "bookings", "revenue"])
    names = ads.sort_values("date").groupby("campaign_id")["campaign_name"].last()
    g = ads.groupby("campaign_id").agg(cost=("cost", "sum"), clicks=("clicks", "sum"), impressions=("impressions", "sum"))
    g["campaign"] = names
    leads = M._leads(session, d1, d2)
    if not leads.empty:
        dl = leads[leads["is_direct"] & ~leads["is_spam"] & leads["campaign_id"].notna()].copy()
        dl["campaign_id"] = dl["campaign_id"].astype(int)
        lg = dl.groupby("campaign_id").agg(leads=("id", "count"), bookings=("is_booked", "sum"))
        lg["revenue"] = dl[dl["is_booked"]].groupby("campaign_id")["amount"].sum()
        g = g.join(lg, how="left")
    for c in ("leads", "bookings", "revenue"):
        if c not in g:
            g[c] = 0
        g[c] = g[c].fillna(0)
    g = g.reset_index()
    g["campaign_id"] = g["campaign_id"].astype(int)
    g[["leads", "bookings"]] = g[["leads", "bookings"]].astype(int)
    return g


def div(a, b):
    return round(float(a) / float(b), 4) if b else None


def prev_period(d1: date, d2: date) -> tuple[date, date]:
    n = (d2 - d1).days + 1
    return d1 - timedelta(days=n), d1 - timedelta(days=1)
