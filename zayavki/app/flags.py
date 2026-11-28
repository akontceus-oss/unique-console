"""Подсветка строк: что менеджеру нужно сделать по заявке прямо сейчас."""
from datetime import datetime, timedelta

from .config import get_settings
from .assign import shift_day
from .models import BOOKED_STATUS, CLOSED_STATUSES, OPEN_STATUSES, STALE_HOUR, WAITING_STATUSES, Lead

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


def board_group(lead: Lead, at: datetime) -> str:
    """Колонка доски заявок: new — новая, act — нужно действие, wait — отработана, ждём клиента, closed — бронь или отказ."""
    if lead.status == BOOKED_STATUS or lead.status in CLOSED_STATUSES:
        return "closed"
    if lead.bumped_at and (lead.touched_at is None or lead.touched_at < lead.bumped_at):
        return "act"                                   # клиент написал ещё раз
    if lead.status == "новая" and lead.last_action_at is None:
        return "new"
    if lead.next_contact_at and lead.next_contact_at > at:
        return "wait"                                  # следующий контакт назначен на потом
    done = lead.last_action_at
    if done and shift_day(done) == shift_day(at) and (lead.next_contact_at is None or done >= lead.next_contact_at):
        return "wait"                                  # сегодня с ней уже поработали
    return "act"


def is_alert(lead: Lead, at: datetime) -> bool:
    """Новая заявка висит без ответа дольше порога: подсветка ярче."""
    return (
        lead.status == "новая"
        and lead.taken_at is None
        and lead.created_at <= at - timedelta(minutes=get_settings().new_lead_alert_minutes)
    )


def _touched_after(lead: Lead, moment: datetime) -> bool:
    return bool(lead.touched_at and lead.touched_at >= moment)


def is_stale(lead: Lead, at: datetime) -> bool:
    """Заявка в ожидании, которую со вчера никто не трогал, а уже 14:00 — пусть мигает."""
    if lead.status not in WAITING_STATUSES or at.hour < STALE_HOUR:
        return False
    last = lead.touched_at or lead.created_at
    return last < datetime.combine(at.date(), datetime.min.time())
