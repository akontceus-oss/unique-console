"""Расчёт показателей для дашборда, отчёта и правил.

Главная метрика — цена брони: дешёвые заявки без броней только съедают бюджет.
Цена заявки и брони считаются по заявкам, пришедшим из Директа.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_rules
from ..models import Action, AdStat, CampaignSnapshot, Lead, SeoKeyword, SeoPosition, SettingsChange, Touch, WebStat
from .normalize import mask_phone, yesterday

DIRECT_LABELS = ("Директ · Поиск", "Директ · РСЯ")


# ------------------------------------------------------------ helpers ---
def period(days: int = 30, end: date | None = None) -> tuple[date, date]:
    end = end or yesterday()
    return end - timedelta(days=days - 1), end


def _df(session: Session, stmt) -> pd.DataFrame:
    df = pd.read_sql(stmt, session.connection())
    for col in df.columns:
        if df[col].dtype == object and len(df) and df[col].map(lambda v: hasattr(v, "as_integer_ratio")).all():
            df[col] = df[col].astype(float)
    return df


def _text(v) -> str:
    return v if isinstance(v, str) else ""  # None и NaN из pandas — пустая строка


def _div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def _booked() -> set[str]:
    return set(load_rules()["leads"]["booked_statuses"])


def _ad_stats(session, d1, d2) -> pd.DataFrame:
    df = _df(session, select(AdStat).where(AdStat.date.between(d1, d2)))
    if not df.empty:
        df["cost"] = df["cost"].astype(float)
    return df


def _leads(session, d1, d2) -> pd.DataFrame:
    stmt = select(Lead).where(Lead.first_touch_at >= pd.Timestamp(d1), Lead.first_touch_at < pd.Timestamp(d2 + timedelta(days=1)))
    df = _df(session, stmt)
    if not df.empty:
        df["amount"] = df["amount"].astype(float)
        df["date"] = pd.to_datetime(df["first_touch_at"]).dt.date
        df["is_direct"] = df["source_label"].isin(DIRECT_LABELS)
        df["is_booked"] = df["status"].isin(_booked())
        df["is_spam"] = df["status"] == "spam"
    return df


def campaign_names(session: Session) -> dict[int, str]:
    """Последнее название каждой кампании (кампании переименовывают)."""
    rows = session.execute(select(AdStat.campaign_id, AdStat.campaign_name, AdStat.date).order_by(AdStat.date)).all()
    return {int(cid): name for cid, name, _ in rows}


# ------------------------------------------------------------ сводка ---
def _totals(session, d1, d2) -> dict:
    ads = _ad_stats(session, d1, d2)
    leads = _leads(session, d1, d2)
    cost = float(ads["cost"].sum()) if not ads.empty else 0.0
    clicks = int(ads["clicks"].sum()) if not ads.empty else 0
    impressions = int(ads["impressions"].sum()) if not ads.empty else 0
    if leads.empty:
        all_leads = direct_leads = direct_booked = all_booked = 0
        revenue = 0.0
    else:
        real = leads[~leads["is_spam"]]
        all_leads = int(len(real))
        direct = real[real["is_direct"]]
        direct_leads = int(len(direct))
        direct_booked = int(direct["is_booked"].sum())
        all_booked = int(real["is_booked"].sum())
        revenue = float(direct.loc[direct["is_booked"], "amount"].sum())
    return {
        "cost": round(cost, 2),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": _div(clicks, impressions),
        "cpc": _div(cost, clicks),
        "leads_all": all_leads,
        "leads_direct": direct_leads,
        "bookings_direct": direct_booked,
        "bookings_all": all_booked,
        "revenue_direct": round(revenue, 2),
        "cpl": _div(cost, direct_leads),
        "cpb": _div(cost, direct_booked),
        "lead_to_booking": _div(direct_booked, direct_leads),
        "romi": _div(revenue - cost, cost),
    }


def summary(session: Session, d1: date, d2: date) -> dict:
    days = (d2 - d1).days + 1
    prev_d2 = d1 - timedelta(days=1)
    prev_d1 = prev_d2 - timedelta(days=days - 1)
    cur, prev = _totals(session, d1, d2), _totals(session, prev_d1, prev_d2)
    change = {k: (_div(cur[k] - prev[k], prev[k]) if isinstance(cur[k], (int, float)) and prev[k] else None) for k in cur}

    ads = _ad_stats(session, d1, d2)
    leads = _leads(session, d1, d2)
    daily = []
    for i in range(days):
        d = d1 + timedelta(days=i)
        day_ads = ads[ads["date"] == d] if not ads.empty else ads
        day_leads = leads[(leads["date"] == d) & leads["is_direct"] & ~leads["is_spam"]] if not leads.empty else leads
        daily.append(
            {
                "date": d.isoformat(),
                "cost": round(float(day_ads["cost"].sum()), 2) if not day_ads.empty else 0.0,
                "leads": int(len(day_leads)),
                "bookings": int(day_leads["is_booked"].sum()) if not day_leads.empty else 0,
            }
        )
    by_source = []
    if not leads.empty:
        real = leads[~leads["is_spam"]]
        for label, g in real.groupby("source_label"):
            by_source.append({"source": label, "leads": int(len(g)), "bookings": int(g["is_booked"].sum())})
        by_source.sort(key=lambda x: -x["leads"])
    return {
        "period": {"from": d1.isoformat(), "to": d2.isoformat(), "days": days},
        "previous": {"from": prev_d1.isoformat(), "to": prev_d2.isoformat()},
        "totals": cur,
        "previous_totals": prev,
        "change": change,
        "daily": daily,
        "by_source": by_source,
        "targets": load_rules()["economics"],
    }


# ------------------------------------------------------------ площадки ---
def _excluded_pairs(session: Session) -> set[tuple[int, str]]:
    from ..rules.state import excluded_pairs  # локальный импорт: rules зависят от metrics

    return excluded_pairs(session)


def goal_totals(goals_column) -> dict[str, int]:
    """Складывает столбец WebStat.goals (словари по строкам) в {"id цели": достижения}."""
    out: dict[str, int] = {}
    for g in goals_column:
        for k, v in (g or {}).items():
            out[str(k)] = out.get(str(k), 0) + int(v or 0)
    return out


def placements(session: Session, d1: date, d2: date) -> list[dict]:
    rules = load_rules()
    target_cpl = rules["economics"]["target_cpl"]
    ads = _ad_stats(session, d1, d2)
    if ads.empty:
        return []
    ads = ads[ads["network"] == "AD_NETWORK"]
    web = _df(session, select(WebStat).where(WebStat.date.between(d1, d2)))
    leads = _leads(session, d1, d2)
    excluded = _excluded_pairs(session)
    pending = {
        a.target.lower()
        for a in session.scalars(select(Action).where(Action.kind == "exclude_site", Action.status == "proposed"))
    }

    out = []
    for placement, g in ads.groupby("placement"):
        key = str(placement).lower()
        spent = {int(c) for c in g.loc[g["cost"] > 0, "campaign_id"]} or {int(c) for c in g["campaign_id"]}
        excluded_in = {c for c in spent if (c, key) in excluded}
        cost, clicks, impr = float(g["cost"].sum()), int(g["clicks"].sum()), int(g["impressions"].sum())
        w = web[web["placement"] == key] if not web.empty else web
        visits = int(w["visits"].sum()) if not w.empty else 0
        bounces = int(w["bounces"].sum()) if not w.empty else 0
        dur = int(w["duration_sum"].sum()) if not w.empty else 0
        goals = goal_totals(w["goals"]) if not w.empty and "goals" in w else {}
        pl = leads[(leads["placement"] == key) & ~leads["is_spam"]] if not leads.empty else leads
        n_leads = int(len(pl))
        n_booked = int(pl["is_booked"].sum()) if not pl.empty else 0
        cpl = _div(cost, n_leads)
        if excluded_in and excluded_in == spent:
            status = "excluded"
        elif key in pending:
            status = "proposed"
        elif n_leads and cpl is not None and cpl <= target_cpl:
            status = "good"
        elif cost >= target_cpl and not n_leads:
            status = "bad"
        else:
            status = "watch"
        out.append(
            {
                "placement": key,
                "campaigns": sorted({str(n) for n in g["campaign_name"]}),
                "impressions": impr,
                "clicks": clicks,
                "ctr": _div(clicks, impr),
                "cost": round(cost, 2),
                "visits": visits,
                "bounce_rate": _div(bounces, visits),
                "avg_duration": _div(dur, visits),
                "goal_reaches": sum(goals.values()),
                "goals": goals,
                "leads": n_leads,
                "bookings": n_booked,
                "cpl": cpl,
                "cpb": _div(cost, n_booked),
                "status": status,
            }
        )
    out.sort(key=lambda r: -r["cost"])
    return out


# ------------------------------------------------------------ кампании ---
def campaigns(session: Session, d1: date, d2: date) -> list[dict]:
    ads = _ad_stats(session, d1, d2)
    if ads.empty:
        return []
    leads = _leads(session, d1, d2)
    total_cost = float(ads["cost"].sum())
    dl = leads[leads["is_direct"] & ~leads["is_spam"]] if not leads.empty else leads
    total_booked = int(dl["is_booked"].sum()) if not dl.empty else 0
    out = []
    names = ads.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
    for cid, g in ads.groupby("campaign_id"):
        name = names[cid]
        cost, clicks = float(g["cost"].sum()), int(g["clicks"].sum())
        cl = dl[dl["campaign_id"] == cid] if not dl.empty else dl
        n_leads = int(len(cl))
        n_booked = int(cl["is_booked"].sum()) if not cl.empty else 0
        revenue = float(cl.loc[cl["is_booked"], "amount"].sum()) if not cl.empty else 0.0
        networks = set(g["network"])
        out.append(
            {
                "campaign_id": int(cid),
                "campaign": name,
                "type": "РСЯ" if networks == {"AD_NETWORK"} else ("Поиск" if networks == {"SEARCH"} else "Поиск + РСЯ"),
                "cost": round(cost, 2),
                "clicks": clicks,
                "leads": n_leads,
                "bookings": n_booked,
                "revenue": round(revenue, 2),
                "cpl": _div(cost, n_leads),
                "cpb": _div(cost, n_booked),
                "cost_share": _div(cost, total_cost),
                "bookings_share": _div(n_booked, total_booked),
            }
        )
    out.sort(key=lambda r: -r["cost"])
    return out


# ------------------------------------------------------------ заявки ---
STATUS_RU = {
    "new": "Новая",
    "in_progress": "В работе",
    "booked": "Бронь",
    "prepaid": "Предоплата",
    "done": "Праздник прошёл",
    "lost": "Отказ",
    "spam": "Спам",
}
CHANNEL_RU = {"call": "Звонок", "form": "Форма", "site_form": "Форма сайта", "chat": "Чат", "crm": "CRM", "booking": "Бронь YCLIENTS"}


def leads_list(session: Session, d1: date, d2: date, limit: int = 500) -> list[dict]:
    names = campaign_names(session)
    leads = _leads(session, d1, d2)
    if leads.empty:
        return []
    leads = leads.sort_values("first_touch_at", ascending=False).head(limit)
    out = []
    for r in leads.itertuples():
        out.append(
            {
                "id": int(r.id),
                "created_at": pd.Timestamp(r.first_touch_at).strftime("%Y-%m-%d %H:%M"),
                "phone": mask_phone(r.phone),
                "channel": CHANNEL_RU.get(r.channel, r.channel),
                "source": r.source_label,
                "campaign": names.get(int(r.campaign_id), str(int(r.campaign_id))) if pd.notna(r.campaign_id) else "",
                "placement": r.placement if isinstance(r.placement, str) else "",
                "keyword": r.keyword if isinstance(r.keyword, str) else "",
                "status": STATUS_RU.get(r.status, r.status),
                "status_code": r.status,
                "amount": float(r.amount or 0),
                "party_date": r.party_date.isoformat() if isinstance(r.party_date, date) else "",
                "guests": int(r.guests) if pd.notna(r.guests) else None,
                "manager": r.manager if isinstance(r.manager, str) else "",
                "touches": int(r.touches_count),
            }
        )
    return out


# ------------------------------------------------------------ обработка ---
def processing(session: Session, d1: date, d2: date) -> dict:
    t = _df(
        session,
        select(Touch).where(Touch.created_at >= pd.Timestamp(d1), Touch.created_at < pd.Timestamp(d2 + timedelta(days=1))),
    )
    leads = _leads(session, d1, d2)
    calls = t[t["source"] == "call"] if not t.empty else t
    chats = t[t["source"] == "chat"] if not t.empty else t
    n_calls = int(len(calls))
    n_missed = int((calls["answered"] == False).sum()) if n_calls else 0  # noqa: E712
    resp = chats["response_sec"].dropna() if not chats.empty else pd.Series(dtype=float)

    funnel = []
    managers = []
    lost_leads = 0
    if not leads.empty:
        real = leads[~leads["is_spam"]]
        order = ["new", "in_progress", "booked", "prepaid", "done"]
        for i, st in enumerate(order):
            reached = int(real["status"].isin(order[i:]).sum()) if st != "new" else int(len(real))
            funnel.append({"stage": STATUS_RU[st], "code": st, "count": reached})
        funnel.append({"stage": STATUS_RU["lost"], "code": "lost", "count": int((real["status"] == "lost").sum())})
        lost_leads = int(real["missed_call"].sum())
        for m, g in real[real["manager"].notna()].groupby("manager"):
            managers.append(
                {
                    "manager": m,
                    "leads": int(len(g)),
                    "bookings": int(g["is_booked"].sum()),
                    "conversion": _div(int(g["is_booked"].sum()), int(len(g))),
                    "revenue": round(float(g.loc[g["is_booked"], "amount"].sum()), 2),
                }
            )
        managers.sort(key=lambda x: -x["leads"])
    return {
        "calls": n_calls,
        "missed_calls": n_missed,
        "missed_share": _div(n_missed, n_calls),
        "lost_after_missed": lost_leads,
        "chats": int(len(chats)),
        "chat_response_median_sec": float(resp.median()) if len(resp) else None,
        "chat_response_p90_sec": float(resp.quantile(0.9)) if len(resp) else None,
        "funnel": funnel,
        "managers": managers,
    }


# ------------------------------------------------------------ сверка броней ---
NO_SOURCE = "Прямые / неизвестно"


def booking_reconciliation(session: Session, d1: date, d2: date) -> list[dict]:
    """Брони YCLIENTS по источникам: откуда пришёл клиент, который записался.

    Бронь привязана к заявке по телефону (склейка заявок). Группа: «Директ», «Другие источники»
    или «Без источника» — бронь без обращения с метками (пришёл сам, позвонил на номер без подмены).
    """
    leads = _leads(session, d1, d2)
    if leads.empty:
        return []
    yc = leads[leads["crm_external_id"].fillna("").str.startswith("booking:") & leads["is_booked"]]
    if yc.empty:
        return []
    total = len(yc)
    rows = []
    for label, g in yc.groupby("source_label"):
        group = "Директ" if label in DIRECT_LABELS else ("Без источника" if label == NO_SOURCE else "Другие источники")
        rows.append({"group": group, "source": label, "bookings": int(len(g)), "revenue": round(float(g["amount"].sum()), 2),
                     "share": round(len(g) / total, 4), "with_touch": int((g["touches_count"] > 0).sum())})
    order = {"Директ": 0, "Другие источники": 1, "Без источника": 2}
    rows.sort(key=lambda r: (order[r["group"]], -r["bookings"]))
    return rows


# ------------------------------------------------------------ SEO ---
# Доля кликов по месту в выдаче (усреднённо по открытым исследованиям CTR). Нужна, чтобы оценить
# переходы и видимость: 1-е место собирает примерно треть кликов, за пределами топ-20 кликов почти нет.
CTR = {1: 0.30, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.02}
HISTORY_DATES = 8  # сколько последних проверок показывать в таблице запросов
IMPORTANT_TOP = 30  # «важные» — самые частотные запросы


def ctr(pos: int | None) -> float:
    if not pos:
        return 0.0
    return CTR.get(pos) or (0.01 if pos <= 20 else 0.0)


def _engine(name: str) -> str:
    n = (name or "").lower()
    return "google" if "google" in n else "yandex" if ("yandex" in n or "яндекс" in n) else "other"


def _stats(cells: list[tuple[int | None, float]]) -> dict:
    """cells — (позиция, вес запроса). Видимость: доля кликов, которую сайт собирает от максимума (все на 1-м месте)."""
    found = [p for p, _ in cells if p]
    full = sum(w for _, w in cells) * CTR[1]
    return {
        "total": len(cells),
        "found": len(found),
        "top3": sum(1 for p in found if p <= 3),
        "top10": sum(1 for p in found if p <= 10),
        "top30": sum(1 for p in found if p <= 30),
        "avg": round(sum(found) / len(found), 1) if found else None,
        "visibility": round(sum(w * ctr(p) for p, w in cells) / full, 4) if full else None,
    }


def seo(session: Session, d1: date, d2: date) -> dict:
    """Позиции из Топвизора: регионы (поисковик × регион × устройство), запросы с историей, точки роста, группы.

    Сравнение — с предыдущей проверкой в том же регионе (Топвизор проверяет по своему расписанию, а не каждый день).
    """
    df = _df(session, select(SeoPosition))
    leads = _leads(session, d1, d2)
    organic = int((leads["source_label"] == "SEO").sum()) if not leads.empty else 0
    empty = {"date": None, "keywords": [], "top3": 0, "top10": 0, "avg_position": None, "visibility": None, "organic_leads": organic,
             "history": [], "regions": [], "groups": [], "growth": [], "dates": [], "has_volume": False}
    if df.empty:
        return empty
    vols = {(_text(v.project), v.keyword): v for v in session.scalars(select(SeoKeyword))}
    has_volume = any((v.volume_yandex or v.volume_google) for v in vols.values())

    def volume(kw: str, project: str, engine: str) -> int | None:
        v = vols.get((project, kw)) or next((x for (p, k), x in vols.items() if k == kw), None)
        if not v:
            return None
        return v.volume_google if engine == "google" else v.volume_yandex

    df["region_name"] = [_text(n) or _text(r) for n, r in zip(df["region_name"], df["region"])]
    df["engine"] = df["region_name"].map(_engine)
    df["position"] = df["position"].astype("float")
    order = {"yandex": 0, "google": 1, "other": 2}
    regions = sorted({(r, n, e) for r, n, e in zip(df["region"], df["region_name"], df["engine"])}, key=lambda x: (order[x[2]], x[1]))
    pos_of: dict[tuple, dict] = {}  # (регион, запрос) → {дата: позиция}
    url_of: dict[tuple, str] = {}
    for r in df.itertuples():
        pos_of.setdefault((r.region, r.keyword), {})[r.date] = int(r.position) if pd.notna(r.position) else None
        if _text(r.url):
            url_of[(r.region, r.keyword, r.date)] = r.url
    meta = df.sort_values("date").groupby("keyword").last()
    all_dates = sorted(df["date"].unique())
    last = max(all_dates)

    def weight(kw: str, project: str, engine: str) -> float:
        if not has_volume:
            return 1.0
        return float(volume(kw, project, engine) or 0)

    reg_out, history = [], []
    for key, name, engine in regions:
        sub = df[df["region"] == key]
        dates = sorted(sub["date"].unique())
        cur_d = dates[-1]
        prev_d = dates[-2] if len(dates) > 1 else None
        for d in dates[-60:]:
            g = sub[sub["date"] == d]
            st = _stats([(int(p) if pd.notna(p) else None, weight(k, _text(pr), engine)) for p, k, pr in zip(g["position"], g["keyword"], g["project"])])
            history.append({"date": d.isoformat(), "region": key, "region_name": name, "engine": engine, **st})
        cur = next(h for h in reversed(history) if h["region"] == key and h["date"] == cur_d.isoformat())
        prev = next((h for h in history if h["region"] == key and prev_d and h["date"] == prev_d.isoformat()), None)
        reg_out.append({"region": key, "name": name, "engine": engine, "date": cur_d.isoformat(),
                        "prev_date": prev_d.isoformat() if prev_d else None, "checks": len(dates),
                        **{k: cur[k] for k in ("total", "found", "top3", "top10", "top30", "avg", "visibility")},
                        **{f"prev_{k}": (prev or {}).get(k) for k in ("top3", "top10", "avg", "visibility")}})

    show_dates = all_dates[-HISTORY_DATES:]
    keywords = []
    for kw, m in meta.iterrows():
        project = _text(m["project"])
        cells, best, traffic = [], None, 0.0
        for key, name, engine in regions:
            hist = pos_of.get((key, kw))
            if hist is None:
                cells.append(None)
                continue
            dates = sorted(hist)
            pos = hist[dates[-1]]
            prev = hist[dates[-2]] if len(dates) > 1 else None
            vol = volume(kw, project, engine)
            traffic += (vol or 0) * ctr(pos)
            best = pos if (pos and (best is None or pos < best)) else best
            cells.append({"region": key, "engine": engine, "pos": pos, "prev": prev, "date": dates[-1].isoformat(),
                          "change": (prev - pos) if (pos and prev) else None, "volume": vol,
                          "url": url_of.get((key, kw, dates[-1]), ""),
                          "hist": [hist.get(d, "") if d in hist else "" for d in show_dates]})
        vy, vg = volume(kw, project, "yandex"), volume(kw, project, "google")
        keywords.append({"keyword": kw, "group": _text(m["group_name"]), "project": project,
                         "volume": vy if vy is not None else vg, "volume_yandex": vy, "volume_google": vg,
                         "best": best, "traffic": round(traffic, 1), "cells": cells,
                         "url": next((c["url"] for c in cells if c and c["url"]), "")})
    by_volume = sorted((k for k in keywords if k["volume"]), key=lambda k: -k["volume"])
    important = {k["keyword"] for k in by_volume[:IMPORTANT_TOP]}
    for k in keywords:
        k["important"] = k["keyword"] in important
    keywords.sort(key=lambda k: (-(k["volume"] or 0), k["best"] or 999, k["keyword"]))

    # Точки роста: где подъём даст больше всего переходов. Цель — топ-3; без частотности считаем каждый запрос равным.
    growth = []
    for k in keywords:
        for c in k["cells"]:
            if not c or (c["pos"] and c["pos"] <= 3):
                continue
            w = (c["volume"] or 0) if has_volume else 1
            gain = w * (CTR[3] - ctr(c["pos"]))
            if gain <= 0:
                continue
            p = c["pos"]
            stage = "near" if p and p <= 10 else "close" if p and p <= 20 else "far" if p else "absent"
            growth.append({"keyword": k["keyword"], "group": k["group"], "region": c["region"],
                           "region_name": next(n for r, n, _ in regions if r == c["region"]), "engine": c["engine"],
                           "position": p, "change": c["change"], "volume": c["volume"], "stage": stage,
                           "gain": round(gain, 1), "url": c["url"]})
    # порядок — по переходам с поправкой на усилия: запрос с 6-го места поднять проще, чем вывести из-за сотни
    ease = {"near": 1.0, "close": 0.7, "far": 0.35, "absent": 0.25}
    growth.sort(key=lambda g: (-g["gain"] * ease[g["stage"]], g["position"] or 999))

    groups = []
    for gname in sorted({k["group"] for k in keywords}):
        ks = [k for k in keywords if k["group"] == gname]
        row = {"group": gname or "Без группы", "keywords": len(ks), "volume": sum(k["volume"] or 0 for k in ks)}
        for eng in ("yandex", "google"):
            cells = [(c["pos"], ((c["volume"] or 0) if has_volume else 1.0)) for k in ks for c in k["cells"] if c and c["engine"] == eng]
            st = _stats(cells) if cells else {}
            row[f"{eng}_avg"] = st.get("avg")
            row[f"{eng}_top10"] = st.get("top10")
            row[f"{eng}_total"] = st.get("total")
            row[f"{eng}_visibility"] = st.get("visibility")
        groups.append(row)
    groups.sort(key=lambda g: -g["volume"] if has_volume else -g["keywords"])

    cur_cells = [(c["pos"], 1.0) for k in keywords for c in k["cells"] if c and c["date"] == max(x["date"] for x in reg_out if x["region"] == c["region"])]
    total = _stats(cur_cells)
    vis = [r["visibility"] for r in reg_out if r["visibility"] is not None]
    return {
        "date": last.isoformat(),
        "dates": [d.isoformat() for d in show_dates],
        "regions": reg_out,
        "keywords": keywords,
        "growth": growth,
        "groups": groups,
        "history": history,
        "has_volume": has_volume,
        "top3": total["top3"],
        "top10": total["top10"],
        "avg_position": total["avg"],
        "visibility": round(sum(vis) / len(vis), 4) if vis else None,
        "organic_leads": organic,
    }


# ------------------------------------------------------------ журнал ---
ACTION_STATUS_RU = {
    "proposed": "Ждёт решения",
    "approved": "Одобрено",
    "applied": "Применено",
    "rejected": "Отклонено",
    "rolled_back": "Откачено",
    "failed": "Ошибка",
    "alert": "Сигнал",
}
KIND_RU = {"exclude_site": "Исключить площадку", "add_negative": "Минус-слово", "bid_modifier": "Корректировка ставки",
           "set_cpa": "Целевая цена конверсии", "alert": "Сигнал"}


def journal(session: Session, d1: date, d2: date, limit: int = 300) -> list[dict]:
    out = []
    start = pd.Timestamp(d1).to_pydatetime()
    for a in session.scalars(select(Action).where(Action.created_at >= start).order_by(Action.created_at.desc()).limit(limit)):
        out.append(
            {
                "id": a.id,
                "at": a.created_at.strftime("%Y-%m-%d %H:%M"),
                "author": "Платформа" if a.level == "auto" else ("Платформа → вы" if a.level == "approval" else "Платформа"),
                "kind": KIND_RU.get(a.kind, a.kind),
                "kind_code": a.kind,
                "level": a.level,
                "target": a.target,
                "campaigns": ", ".join(a.campaign_names or []),
                "reason": a.reason,
                "status": ACTION_STATUS_RU.get(a.status, a.status),
                "status_code": a.status,
                "mode": a.apply_mode or "",
                "effect": a.eval_result or None,
            }
        )
    # Правки самой платформы уже есть в журнале как действия — из снимков показываем только внешние.
    for c in session.scalars(
        select(SettingsChange)
        .where(SettingsChange.date >= d1, SettingsChange.author == "external")
        .order_by(SettingsChange.date.desc())
        .limit(limit)
    ):
        out.append(
            {
                "id": f"c{c.id}",
                "at": c.date.isoformat() + " 00:00",
                "author": "Вне платформы",
                "kind": f"Изменено: {c.field}",
                "kind_code": "settings_change",
                "level": "observed",
                "target": (c.after or "")[:120],
                "campaigns": c.campaign_name,
                "reason": f"было: {(c.before or '—')[:120]}",
                "status": "Замечено",
                "status_code": "observed",
                "mode": "",
                "effect": None,
            }
        )
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:limit]
