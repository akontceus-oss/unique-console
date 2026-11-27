"""Брони комнат: журнал и правила (ТЗ: claude/tz-zhurnal-zapisi.md в проекте).

- Праздник 3 часа (кнопкой «+3 ч» — 6, 9… до закрытия), после него уборка 15 минут.
- Одна бронь может занять 2–3 комнаты (booking_room «Монако + Сингапур»), все с одного времени.
- В одной комнате следующий праздник не раньше чем через час после конца предыдущего.
- Старты в разных комнатах отличаются минимум на 30 минут.
- Центр открыт с 10:00 до 22:00.
- Выходные: у каждой комнаты «цепочка» с шагом 4 часа — 10:00/14:00/18:00, 10:30/14:30/18:30
  или 11:00/15:00/19:00. Первая бронь дня в комнате закрепляет цепочку, у разных комнат цепочки разные.
- Будни: время свободное, предупреждаем о «мёртвом окне», куда не влезет ещё один праздник.
"""
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import datetime, timedelta

from .clock import now
from .models import BOOKED_STATUS, CLOSED_STATUSES, HOLD_HOURS, ROOMS, Lead

PARTY = 180      # длительность праздника, мин
CLEANING = 15    # уборка после праздника
ROOM_GAP = 60    # от конца праздника до следующего в той же комнате
STAGGER = 30     # минимальная разница стартов в разных комнатах
OPEN, CLOSE = 10 * 60, 22 * 60
CHAIN_STEP = PARTY + ROOM_GAP           # 4 часа
CHAIN_OFFSETS = (0, 30, 60)             # цепочки выходного: старт в 10:00, 10:30, 11:00
WEEKEND = (5, 6)


def parse_hm(v: str) -> int | None:
    """«14:30», «14.30», «14-30» → минуты от полуночи; иначе None."""
    m = re.match(r"^\s*(\d{1,2})[:.\-](\d{2})", v or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return int(m[1]) * 60 + int(m[2])


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


ROOM_SEP = " + "


def rooms_of(value: str | None) -> list[str]:
    """«Сингапур + Монако» → ["Монако", "Сингапур"] (в порядке ROOMS, неизвестное отбрасываем)."""
    picked = {x.strip() for x in (value or "").split("+")}
    return [r for r in ROOMS if r in picked]


def lead_minutes(lead: Lead) -> int:
    return (lead.booking_hours or 3) * 60


def is_paid(lead: Lead) -> bool:
    return bool(lead.prepaid_amount) or lead.status == BOOKED_STATUS


def hold(lead: Lead, at: datetime | None = None) -> dict | None:
    """Бронь без предоплаты: до какого времени держим и вышел ли срок. Оплачена или брони нет — None."""
    if is_paid(lead) or lead.status in CLOSED_STATUSES or not (lead.booking_date and lead.booking_time and lead.booking_room):
        return None
    since = lead.booking_at or lead.created_at
    until = since + timedelta(hours=HOLD_HOURS)
    return {"until": until.isoformat(timespec="minutes"), "expired": (at or now()) >= until,
            "left_hours": max(0, int(((until - (at or now())).total_seconds() + 3599) // 3600))}


@dataclass
class Booking:
    lead_id: int
    room: str
    start: int
    minutes: int = PARTY
    soft: bool = False      # без предоплаты и сутки прошли — время можно отдать другому

    @property
    def end(self) -> int:
        return self.start + self.minutes


def day_leads(s: Session, day: date) -> list[Lead]:
    """Заявки с бронью комнат на этот день, без отказов и нецелевых."""
    q = select(Lead).where(Lead.booking_date == day, Lead.booking_room != "", Lead.status.not_in(CLOSED_STATUSES))
    return [l for l in s.scalars(q) if parse_hm(l.booking_time) is not None and rooms_of(l.booking_room)]


def day_bookings(s: Session, day: date, exclude_id: int | None = None) -> list[Booking]:
    """Брони дня по комнатам: бронь на две комнаты — две записи."""
    return sorted((Booking(l.id, r, parse_hm(l.booking_time), lead_minutes(l), bool((hold(l) or {}).get("expired")))
                   for l in day_leads(s, day) if l.id != exclude_id for r in rooms_of(l.booking_room)),
                  key=lambda b: b.start)


def _offset(start: int) -> int | None:
    """Номер цепочки выходного для времени старта или None, если время вне сетки."""
    k, off = divmod(start - OPEN, CHAIN_STEP)
    return off if off in CHAIN_OFFSETS and 0 <= k <= 2 else None


def room_chains(bookings: list[Booking]) -> dict[str, int]:
    """Цепочка каждой комнаты в выходной — по её первой брони (если та в сетке)."""
    out = {}
    for b in sorted(bookings, key=lambda b: b.start):
        off = _offset(b.start)
        if b.room not in out and off is not None:
            out[b.room] = off
    return out


def check(bookings: list[Booking], day: date, room: str, start: int, minutes: int = PARTY, own_rooms: tuple = ()) -> list[dict]:
    """Предупреждения для брони room/start на день. level: red — нарушение, yellow — неудобно."""
    warn = []
    red = lambda t: warn.append({"level": "red", "text": t})  # noqa: E731
    yellow = lambda t: warn.append({"level": "yellow", "text": t})  # noqa: E731
    end = start + minutes
    if start < OPEN or end > CLOSE:
        red(f"Праздник {hm(start)}–{hm(end)} выходит за часы работы 10:00–22:00")

    soft = [b for b in bookings if b.soft]
    for b in soft:                       # срок брони без предоплаты вышел — можно занять, но надо согласовать
        if b.room == room and start < b.end + ROOM_GAP and b.start < end + ROOM_GAP:
            yellow(f"{room}: {hm(b.start)}–{hm(b.end)} держат без предоплаты, сутки прошли — время можно отдать, предупредите менеджера")
    bookings = [b for b in bookings if not b.soft]
    same = [b for b in bookings if b.room == room]
    for b in same:
        if start < b.end and b.start < end:
            red(f"{room} занята: праздник {hm(b.start)}–{hm(b.end)}")
        elif 0 <= start - b.end < ROOM_GAP:
            red(f"Перерыв после праздника {hm(b.start)}–{hm(b.end)} всего {start - b.end} мин, нужен час")
        elif 0 <= b.start - end < ROOM_GAP:
            red(f"Перерыв до праздника в {hm(b.start)} всего {b.start - end} мин, нужен час")
    for b in bookings:
        if b.room != room and abs(b.start - start) < STAGGER:
            red(f"Старт в {hm(start)} слишком близко к старту в {b.room} ({hm(b.start)}): нужно 30 мин разницы")

    if day.weekday() in WEEKEND:
        chains = room_chains(bookings)
        off = _offset(start)
        mine = chains.get(room)
        # комнаты этой же брони (одна компания, один старт) сетку друг другу не занимают
        used = {o: r for r, o in chains.items() if r != room and r not in own_rooms}
        if off is None:
            yellow("Выходной: время вне сетки (10:00/10:30/11:00, +4 ч), в комнате не поместится 3 праздника")
        elif mine is not None and off != mine:
            yellow(f"Выходной: у {room} уже сетка с {hm(OPEN + mine)}, это время её ломает")
        elif mine is None and off in used:
            yellow(f"Выходной: сетка с {hm(OPEN + off)} уже у {used[off]}, двум комнатам на ней не разойтись")
    elif not any(w["level"] == "red" for w in warn):
        # будни: свободное время рядом с бронью, куда уже не влезет ещё один праздник (с часовыми перерывами)
        prev = max((b for b in same if b.end <= start), key=lambda b: b.end, default=None)
        nxt = min((b for b in same if b.start >= end), key=lambda b: b.start, default=None)
        before = (prev.end + ROOM_GAP) if prev else OPEN
        after = (nxt.start - ROOM_GAP) if nxt else CLOSE
        for a, b in ((before, start), (end, after)):
            if ROOM_GAP <= b - a < PARTY + ROOM_GAP:
                yellow(f"Останется {b - a} мин свободного времени ({hm(a)}–{hm(b)}): ещё один праздник туда не влезет")
    return warn


def check_rooms(bookings: list[Booking], day: date, rooms: list[str], start: int, minutes: int = PARTY) -> list[dict]:
    """То же для брони на несколько комнат: свои комнаты друг другу не мешают (одна компания, один старт)."""
    out = []
    for r in rooms:
        for w in check(bookings, day, r, start, minutes, tuple(rooms)):
            if w not in out:
                out.append(w)
    return out


SLOT_TIMES = range(OPEN, CLOSE - PARTY + 1, 30)


def slots(bookings: list[Booking], day: date, room: str, minutes: int = PARTY) -> list[dict]:
    """Все старты с шагом 30 мин для комнаты: уровень предупреждения и входит ли время в сетку выходного."""
    weekend = day.weekday() in WEEKEND
    out = []
    for t in range(OPEN, CLOSE - minutes + 1, 30):
        w = check(bookings, day, room, t, minutes)
        level = "red" if any(x["level"] == "red" for x in w) else ("yellow" if w else None)
        out.append({"time": hm(t), "level": level, "grid": weekend and _offset(t) is not None and level is None})
    return out
