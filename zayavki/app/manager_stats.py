"""Итоги банкетных менеджеров за месяц: конверсия, скорость обработки, качество карточки, отзывы.

Заявки берутся по дате поступления (created_at) и относятся к менеджеру, который ведёт их сейчас.
"""
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BOOKED_STATUS, DELETED_STATUS, PROCESSED_FIELDS, QUALITY_BOOKING_FIELDS, QUALITY_FIELDS,
    UNQUALIFIED_STATUSES, Lead, LeadEvent, User,
)

FIELD_NAMES = {
    "name": "имя", "phone": "телефон", "status": "статус", "source_id": "канал", "child_age": "возраст",
    "holiday_date": "дата праздника", "kids": "детей", "adults": "взрослых", "package": "пакет",
    "manager_comment": "комментарий", "booking_date": "дата брони", "booking_room": "комната",
    "booking_time": "время", "booking_show": "шоу",
}


def _filled(field: str, value) -> bool:
    if field == "status":
        return bool(value) and value != "новая"
    return value not in (None, "", [])


def processed_at(lead: Lead, events: list[LeadEvent]) -> datetime | None:
    """Когда заявка стала обработанной: заполнены имя, телефон, статус, канал и возраст.
    Время каждого поля — первое заполнение (по истории); итог — самое позднее из них."""
    if not all(_filled(f, getattr(lead, f)) for f in PROCESSED_FIELDS):
        return None
    moments = []
    for f in PROCESSED_FIELDS:
        evs = [e for e in events if e.field == f]
        if not evs or _filled(f, evs[0].old):          # было заполнено при создании
            moments.append(lead.created_at)
            continue
        first = next((e.at for e in evs if _filled(f, e.new)), None)
        moments.append(first or lead.created_at)
    return max(moments)


def quality(lead: Lead) -> tuple[int, int, list[str]]:
    fields = QUALITY_FIELDS + (QUALITY_BOOKING_FIELDS if lead.status == BOOKED_STATUS else ())
    missing = [f for f in fields if not _filled(f, getattr(lead, f))]
    return len(fields) - len(missing), len(fields), missing


def month_stats(s: Session, month: date, today: date) -> dict:
    start = datetime(month.year, month.month, 1)
    end = datetime(month.year + (month.month == 12), month.month % 12 + 1, 1)
    leads = s.scalars(select(Lead).where(Lead.created_at >= start, Lead.created_at < end, Lead.status != DELETED_STATUS)).all()
    events: dict[int, list[LeadEvent]] = {}
    if leads:
        for e in s.scalars(select(LeadEvent).where(LeadEvent.lead_id.in_([l.id for l in leads])).order_by(LeadEvent.at, LeadEvent.id)):
            events.setdefault(e.lead_id, []).append(e)

    def block(items: list[Lead]) -> dict:
        qual = [l for l in items if l.status not in UNQUALIFIED_STATUSES]
        booked = [l for l in items if l.status == BOOKED_STATUS]
        waits = []
        for l in items:
            p = processed_at(l, events.get(l.id, []))
            if p:
                waits.append(max(0, (p - l.created_at).total_seconds() / 60))
        got = need = 0
        miss: dict[str, int] = {}
        for l in qual:
            g, n, m = quality(l)
            got, need = got + g, need + n
            for f in m:
                miss[f] = miss.get(f, 0) + 1
        past = [l for l in booked if l.booking_date and l.booking_date < today]
        reviewed = [l for l in past if (l.review or "").strip()]
        pct = lambda a, b: round(a / b * 100) if b else None  # noqa: E731
        return {
            "leads": len(items), "qualified": len(qual), "booked": len(booked),
            "conversion": pct(len(booked), len(qual)),
            "processed": len(waits), "processed_share": pct(len(waits), len(items)),
            "speed_median_min": round(median(waits)) if waits else None,
            "fast_share": pct(sum(1 for w in waits if w <= 60), len(waits)),
            "quality": pct(got, need),
            "often_missing": [FIELD_NAMES.get(f, f) for f, _ in sorted(miss.items(), key=lambda kv: -kv[1])[:3]],
            "holidays_past": len(past), "reviews": len(reviewed), "review_share": pct(len(reviewed), len(past)),
            "prepaid_sum": sum(l.prepaid_amount or 0 for l in booked),
        }

    managers = s.scalars(select(User).where(User.role == "manager").order_by(User.name)).all()
    by_id: dict[int | None, list[Lead]] = {}
    for l in leads:
        by_id.setdefault(l.manager_id, []).append(l)
    rows = [{"id": m.id, "name": m.name, "color": m.color, "active": m.active, **block(by_id.get(m.id, []))}
            for m in managers if m.active or by_id.get(m.id)]
    if by_id.get(None):
        rows.append({"id": None, "name": "без менеджера", "color": "#86868b", "active": True, **block(by_id[None])})
    return {"month": start.date().isoformat(), "total": block(leads), "managers": rows,
            "rules": {"unqualified": sorted(UNQUALIFIED_STATUSES - {DELETED_STATUS}),
                      "processed": [FIELD_NAMES[f] for f in PROCESSED_FIELDS],
                      "quality": [FIELD_NAMES[f] for f in QUALITY_FIELDS],
                      "quality_booking": [FIELD_NAMES[f] for f in QUALITY_BOOKING_FIELDS]}}


def prev_month(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)
