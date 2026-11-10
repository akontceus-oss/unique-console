"""Выгрузка показателей в JSON-файлы (по одному набору строк на файл).

Нужна для внешних дашбордов (DataLens, прототип) и для резервной копии отчётов.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from .pipeline import metrics as M

KPI_LABELS = {
    "cost": "Расход",
    "leads_direct": "Заявки из Директа",
    "bookings_direct": "Брони из Директа",
    "cpl": "Цена заявки",
    "cpb": "Цена брони",
    "lead_to_booking": "Заявка → бронь",
    "revenue_direct": "Выручка с Директа",
    "romi": "ROMI",
    "leads_all": "Все заявки",
    "bookings_all": "Все брони",
    "clicks": "Клики",
    "cpc": "Цена клика",
}


def _change(cur, prev):
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)) or not prev:
        return None
    return round((cur - prev) / prev, 4)


def dashboard_kpi(session: Session, d1: date, d2: date, sm: dict, tr: dict, sources: list[dict]) -> list[dict]:
    """Главные цифры вкладки «Дашборд»: значение, прошлый период, изменение и какое направление — хорошее."""
    from .pipeline.channels import INSTAGRAM, SEARCH

    days = (d2 - d1).days + 1
    p2 = d1 - timedelta(days=1)
    prev_sources = {r["source"]: r for r in M.channels(session, p2 - timedelta(days=days - 1), p2)}
    cur_sources = {r["source"]: r for r in sources}
    g, pg = tr["groups"], tr["previous_groups"]
    out = []

    def add(metric, label, value, previous, good="up"):
        out.append({"metric": metric, "label": label, "value": value, "previous": previous,
                    "change": _change(value, previous), "good": good})

    def visits(*keys):
        return sum(g.get(k, 0) for k in keys), (sum(pg.get(k, 0) for k in keys) if pg is not None else None)

    if tr["has_data"]:
        add("visits_total", "Заходы на сайт", tr["total"], tr["previous_total"])
        add("visits_search_yandex", "Из Яндекс Поиска", *visits("search_yandex"))
        add("visits_search_google", "Из Google Поиска", *visits("search_google"))
        add("visits_search", "Из всех поисковиков", *visits("search_yandex", "search_google", "search_other"))
        add("visits_instagram", "Из Instagram", *visits("instagram"))
    t, pt = sm["totals"], sm["previous_totals"]
    add("leads_all", "Заявки", t["leads_all"], pt["leads_all"])
    add("bookings_all", "Брони", t["bookings_all"], pt["bookings_all"])
    add("cost", "Расход на Директ", t["cost"], pt["cost"], good="none")
    add("cpb", "Цена брони из Директа", t["cpb"], pt["cpb"], good="down")
    for ch, key in ((SEARCH, "search"), (INSTAGRAM, "instagram")):
        c, p = cur_sources.get(ch, {}), prev_sources.get(ch, {})
        add(f"leads_{key}", f"Заявки: {ch}", c.get("leads", 0), p.get("leads", 0))
        add(f"bookings_{key}", f"Брони: {ch}", c.get("bookings", 0), p.get("bookings", 0))
    return out


def datasets(session: Session, d1: date, d2: date) -> dict[str, list[dict]]:
    sm = M.summary(session, d1, d2)
    tr = M.traffic(session, d1, d2)
    sources = M.channels(session, d1, d2)
    kpi = [
        {
            "group": "direct",
            "metric": k,
            "label": label,
            "value": sm["totals"].get(k),
            "previous": sm["previous_totals"].get(k),
            "change": sm["change"].get(k),
        }
        for k, label in KPI_LABELS.items()
    ]
    target_labels = {"target_cpl": "Цель: цена заявки", "target_cpb": "Цель: цена брони", "average_check": "Средний чек"}
    kpi += [{"group": "target", "metric": k, "label": target_labels.get(k, k), "value": v, "previous": None, "change": None}
            for k, v in sm["targets"].items()]
    proc = M.processing(session, d1, d2)
    for metric, label in [
        ("calls", "Звонков"),
        ("missed_calls", "Пропущено звонков"),
        ("missed_share", "Доля пропущенных"),
        ("lost_after_missed", "Не дозвонились совсем"),
        ("chats", "Чатов"),
        ("chat_response_median_sec", "Медиана ответа в чате, с"),
        ("chat_response_p90_sec", "90% ответов быстрее, с"),
    ]:
        kpi.append({"group": "processing", "metric": metric, "label": label, "value": proc[metric], "previous": None, "change": None})
    sq = M.seo(session, d1, d2)
    for metric, label in [("avg_position", "Средняя позиция"), ("top3", "Запросов в топ-3"), ("top10", "Запросов в топ-10"),
                          ("visibility", "Видимость в поиске"), ("organic_leads", "Заявок из поиска")]:
        kpi.append({"group": "seo", "metric": metric, "label": label, "value": sq[metric], "previous": None, "change": None})
    kpi.append({"group": "period", "metric": "period_days", "label": f"{sm['period']['from']} — {sm['period']['to']}",
                "value": sm["period"]["days"], "previous": None, "change": None})
    journal = M.journal(session, d1, d2)
    for j in journal:
        eff = j.pop("effect", None)
        j["effect"] = {"better": "Лучше", "worse": "Хуже", "neutral": "Без изменений"}.get((eff or {}).get("verdict"), "")
        j["id"] = str(j["id"])
    placements = M.placements(session, d1, d2)
    for p in placements:
        p["campaigns"] = ", ".join(p["campaigns"])
    return {
        "kpi": kpi,
        "daily": sm["daily"],
        "sources": sources,
        "dash_kpi": dashboard_kpi(session, d1, d2, sm, tr, sources),
        "traffic_sources": tr["sources"],
        "traffic_daily": tr["daily"],
        "traffic_weekly": tr["weekly"],
        "placements": placements,
        "campaigns": M.campaigns(session, d1, d2),
        "leads": M.leads_list(session, d1, d2),
        "funnel": proc["funnel"],
        "managers": proc["managers"],
        "seo_keywords": sq["keywords"],
        "seo_history": sq["history"],
        "seo_regions": sq["regions"],
        "seo_growth": sq["growth"],
        "seo_groups": sq["groups"],
        "seo_dates": [{"date": d} for d in sq["dates"]],
        "seo_meta": [{"date": sq["date"], "has_volume": sq["has_volume"], "visibility": sq["visibility"]}],
        "journal": journal,
        "booking_sources": M.booking_reconciliation(session, d1, d2),
    }


def export_json(session: Session, d1: date, d2: date, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in datasets(session, d1, d2).items():
        p = out / f"{name}.json"
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        paths.append(p)
    return paths
