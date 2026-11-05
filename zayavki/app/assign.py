"""Распределение заявки на активного банкетного менеджера.

Правило взято из таблицы «Брони»: все заявки дня уходят менеджеру смены.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Lead, Shift, User


@dataclass
class Assignment:
    manager_id: int | None
    late: bool
    reason: str  # repeat / shift / next_shift / none / manual
    repeat_lead_id: int | None = None


def find_repeat(s: Session, phone_norm: str, at: datetime, exclude_id: int | None = None) -> Lead | None:
    if not phone_norm:
        return None
    q = select(Lead).where(Lead.phone_norm == phone_norm).order_by(Lead.created_at.desc())
    if exclude_id:
        q = q.where(Lead.id != exclude_id)
    return s.scalars(q.limit(1)).first()


def shifts_now(s: Session, at: datetime) -> list[Shift]:
    hm = at.strftime("%H:%M")
    q = (
        select(Shift)
        .join(User, User.id == Shift.manager_id)
        .where(Shift.day == at.date(), Shift.start <= hm, Shift.end > hm, User.active.is_(True))
        .order_by(Shift.id)
    )
    return list(s.scalars(q))


def next_shift(s: Session, at: datetime) -> Shift | None:
    hm = at.strftime("%H:%M")
    q = (
        select(Shift)
        .join(User, User.id == Shift.manager_id)
        .where(User.active.is_(True))
        .where(or_(Shift.day > at.date(), and_(Shift.day == at.date(), Shift.start > hm)))
        .order_by(Shift.day, Shift.start, Shift.id)
    )
    return s.scalars(q.limit(1)).first()


def _leads_today(s: Session, manager_id: int, at: datetime) -> int:
    start = datetime.combine(at.date(), datetime.min.time())
    return s.scalar(
        select(func.count(Lead.id)).where(Lead.manager_id == manager_id, Lead.created_at >= start, Lead.created_at < start + timedelta(days=1))
    ) or 0


def pick_manager(s: Session, at: datetime, phone_norm: str = "") -> Assignment:
    cfg = get_settings()
    prev = find_repeat(s, phone_norm, at)
    if prev and prev.manager_id and prev.created_at >= at - timedelta(days=cfg.repeat_client_days):
        mgr = s.get(User, prev.manager_id)
        if mgr and mgr.active:
            return Assignment(mgr.id, False, "repeat", prev.id)

    on_shift = shifts_now(s, at)
    if on_shift:
        # двое на смене: заявку получает тот, у кого сегодня меньше заявок
        best = min(on_shift, key=lambda sh: (_leads_today(s, sh.manager_id, at), sh.id))
        return Assignment(best.manager_id, False, "shift", prev.id if prev else None)

    nxt = next_shift(s, at)
    if nxt:
        return Assignment(nxt.manager_id, True, "next_shift", prev.id if prev else None)
    return Assignment(None, False, "none", prev.id if prev else None)


def next_day_no(s: Session, manager_id: int | None, at: datetime) -> int:
    start = datetime.combine(at.date(), datetime.min.time())
    q = select(func.count(Lead.id)).where(Lead.created_at >= start, Lead.created_at < start + timedelta(days=1))
    q = q.where(Lead.manager_id == manager_id) if manager_id else q.where(Lead.manager_id.is_(None))
    return (s.scalar(q) or 0) + 1
