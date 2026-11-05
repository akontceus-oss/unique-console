"""Подсветка строк: что менеджеру нужно сделать по заявке прямо сейчас."""
from datetime import datetime, timedelta

from .config import get_settings
from .models import CLOSED_STATUSES, OPEN_STATUSES, Lead

# порядок = важность; сверху то, что нужно сделать в первую очередь
FLAGS = {
    "overdue": "Просрочено",
    "new": "Новая",
    "today": "Сегодня",
    "nostep": "Нет следующего шага",
    "soon": "Праздник скоро",
    "review": "Нужен отзыв",
}
FLAG_ORDER = list(FLAGS)


def lead_flag(lead: Lead, at: datetime) -> str | None:
    today = at.date()
    if lead.status == "бронь оплачена":
        event_day = lead.booking_date or lead.holiday_date
        if lead.next_contact_at and lead.next_contact_at <= at and not _touched_after(lead, lead.next_contact_at):
            return "overdue"
        if event_day and today <= event_day <= today + timedelta(days=3):
            return "soon"
        if event_day and event_day < today and not lead.review.strip():
            return "review"
        if lead.next_contact_at and lead.next_contact_at.date() == today:
            return "today"
        return None
    if lead.status in CLOSED_STATUSES:
        return None

    if lead.next_contact_at and lead.next_contact_at <= at and not _touched_after(lead, lead.next_contact_at):
        return "overdue"
    if lead.status == "новая" and lead.taken_at is None:
        return "new"
    if lead.next_contact_at and lead.next_contact_at.date() == today and lead.next_contact_at > at:
        return "today"
    if lead.status in OPEN_STATUSES and (lead.next_contact_at is None or lead.next_contact_at <= at):
        return "nostep"
    if lead.status == "новая" and lead.next_contact_at is None:
        # взяли, но статус не поставили и шаг не назначили
        return "nostep"
    return None


def is_alert(lead: Lead, at: datetime) -> bool:
    """Новая заявка висит без ответа дольше порога: подсветка ярче."""
    return (
        lead.status == "новая"
        and lead.taken_at is None
        and lead.created_at <= at - timedelta(minutes=get_settings().new_lead_alert_minutes)
    )


def _touched_after(lead: Lead, moment: datetime) -> bool:
    return bool(lead.touched_at and lead.touched_at >= moment)
