import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, object_session

from . import auth, booking
from .assign import find_repeat, next_day_no, pick_manager, shift_day, shifts_now
from .clock import now
from .config import get_settings
from .db import get_session, init_engine
from . import tg_booking
from .manager_stats import month_stats, prev_month
from .flags import FLAG_ORDER, FLAGS, is_alert, is_stale, lead_flag
from .models import (
    BOOKING_TIMES,
    CONTACT_CHANNELS,
    DEFAULT_SOURCES,
    HIDDEN_SOURCES,
    SOURCE_RENAMES,
    PACKAGES,
    PRICES,
    PREPAID_PRESETS,
    REFUSAL_REASONS,
    ROLES,
    MANAGER_ROLES,
    SCHEDULE_ROLES,
    STAFF_ROLES,
    TOP_ROLES,
    ROOMS,
    SITE_LABELS,
    SITE_URLS,
    CLOSED_STATUSES,
    DELETED_STATUS,
    SHOWS,
    STATUSES,
    WAITING_STATUSES,
    OLD_STATUS_RENAMES,
    Lead,
    LeadEvent,
    Shift,
    Source,
    User,
)
from .phones import format_phone, normalize_phone

STATIC = Path(__file__).parent / "static"


def create_app(database_url: str | None = None) -> FastAPI:
    init_engine(database_url)
    with _session() as s:
        seed_sources(s)
        for old, new in OLD_STATUS_RENAMES.items():          # «не дозвонились» → «не отвечает»
            s.execute(update(Lead).where(Lead.status == old).values(status=new))
        tg_booking.mark_existing(s)
        backfill_booking_at(s)
        release_closed_bookings(s)
    app = FastAPI(title="Заявки CrazyDrift", docs_url=None, redoc_url=None)
    register(app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


class _session:
    def __enter__(self) -> Session:
        self.s = get_session()
        return self.s

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.s.commit()
        else:
            self.s.rollback()
        self.s.close()


def db():
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def release_booking(s: Session, lead: Lead, user: User | None, at: datetime) -> bool:
    """Снять бронь: время освобождается, бронь уходит в «Удалённые» журнала (оттуда её можно вернуть)."""
    if not (lead.booking_date or lead.booking_time or lead.booking_room):
        return False
    saved = {"booking_date": lead.booking_date.isoformat() if lead.booking_date else "",
             "booking_time": lead.booking_time, "booking_room": lead.booking_room, "booking_hours": lead.booking_hours or 3,
             "booking_room_hours": lead.booking_room_hours or "", "booking_room_packages": lead.booking_room_packages or ""}
    s.add(LeadEvent(lead_id=lead.id, user_id=user.id if user else None, field="booking_deleted",
                    old=json.dumps(saved, ensure_ascii=False), new="", at=at))
    lead.booking_date, lead.booking_time, lead.booking_room, lead.booking_hours, lead.booking_at = None, "", "", None, None
    lead.booking_room_hours = lead.booking_room_packages = ""
    return True


def release_closed_bookings(s: Session) -> None:
    """Отказы, нецелевые и удалённые не держат время в журнале: их брони — в архив (раньше они висели невидимо)."""
    closed = CLOSED_STATUSES - {DELETED_STATUS}           # удалённая заявка вернётся вместе со своей бронью
    for lead in s.scalars(select(Lead).where(Lead.status.in_(closed), Lead.booking_date.is_not(None))):
        release_booking(s, lead, None, now())


def backfill_booking_at(s: Session) -> None:
    """Брони, поставленные до учёта «сутки без предоплаты»: время постановки — из истории (последняя
    правка даты, времени или комнаты брони), иначе — время заявки. Делается один раз: дальше поле заполнено."""
    q = select(Lead).where(Lead.booking_at.is_(None), Lead.booking_date.is_not(None), Lead.booking_room != "")
    for lead in s.scalars(q):
        last = s.scalar(select(func.max(LeadEvent.at)).where(LeadEvent.lead_id == lead.id,
                        LeadEvent.field.in_(("booking_date", "booking_time", "booking_room"))))
        lead.booking_at = last or lead.created_at


def seed_sources(s: Session) -> None:
    """Пустая база получает весь список. В работающую дописываются новые каналы.
    Новый порядок и группы (25.11, с «Поиском Google») применяются один раз — когда «Поиск Google»
    появляется в базе впервые; дальше скрытые и переименованные администратором не трогаем."""
    existing = {name for (name,) in s.execute(select(Source.name))}
    if not existing:
        for i, (group, name) in enumerate(DEFAULT_SOURCES):
            s.add(Source(group=group, name=name, order=i))
        return
    for old, new in SOURCE_RENAMES.items():
        src = s.scalar(select(Source).where(Source.name == old))
        if src and new not in existing:
            src.name = new
            existing.add(new)
    relayout = "Поиск Google" not in existing
    top = s.scalar(select(func.max(Source.order))) or 0
    for group, name in DEFAULT_SOURCES:
        if name not in existing:
            top += 1
            s.add(Source(group=group, name=name, order=top))
    if relayout:
        s.flush()
        by_name = {src.name: src for src in s.scalars(select(Source))}
        for i, (group, name) in enumerate(DEFAULT_SOURCES):
            by_name[name].group, by_name[name].order, by_name[name].active = group, i, True
        for i, name in enumerate(HIDDEN_SOURCES):
            if name in by_name:
                by_name[name].active, by_name[name].order = False, 1000 + i


# ---------------------------------------------------------------- доступ


def current_user(request: Request, s: Session = Depends(db)) -> User:
    payload = auth.read_token(request.cookies.get(auth.COOKIE, ""))
    if not payload:
        raise HTTPException(401, "Нужно войти")
    user = s.get(User, payload["u"])
    if not user or not user.active or user.password_hash[-12:] != payload.get("p"):
        raise HTTPException(401, "Нужно войти")
    return user


def owner(user: User = Depends(current_user)) -> User:
    """Разработчик или директор (удаление заявок, сводка)."""
    if user.role not in TOP_ROLES:
        raise HTTPException(403, "Только для руководителя")
    return user


def staff(user: User = Depends(current_user)) -> User:
    """Администратор, директор или разработчик."""
    if user.role not in STAFF_ROLES:
        raise HTTPException(403, "Только для администратора")
    return user


def scheduler(user: User = Depends(current_user)) -> User:
    """Кто ведёт расписание смен: старший банкетный менеджер, директор, разработчик."""
    if user.role not in SCHEDULE_ROLES:
        raise HTTPException(403, "Расписание ведёт старший банкетный менеджер")
    return user


def can_see(user: User, lead: Lead) -> bool:
    return user.role in STAFF_ROLES or lead.manager_id == user.id


def can_manage(user: User, lead: Lead) -> bool:
    """Править заявку и её бронь. Администратор видит все заявки, но управляет только теми, что создал сам;
    в чужих пишет только комментарий администратора."""
    if user.role == "admin":
        return lead.created_by_id == user.id
    return can_see(user, lead)


# ---------------------------------------------------------------- разбор значений


def parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    v = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise HTTPException(422, f"Не понял дату: {v}")


def parse_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    v = str(v).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            pass
    raise HTTPException(422, f"Не понял дату и время: {v}")


def parse_hm(v: str) -> str:
    try:
        return datetime.strptime(str(v).strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise HTTPException(422, f"Время в формате ЧЧ:ММ: {v}")


def _str(v) -> str:
    return "" if v is None else str(v).strip()


def _room_hours(v) -> str:
    """{"Монако": 6, "Сингапур": 3} или та же строка JSON → строка JSON только с нашими комнатами, 3–12 часов."""
    if isinstance(v, str):
        try:
            v = json.loads(v or "{}")
        except ValueError:
            v = {}
    out = {r: max(3, min(12, _int(h) or 3)) for r, h in (v or {}).items() if r in ROOMS}
    return json.dumps(out, ensure_ascii=False) if out else ""


def _room_packages(v) -> str:
    """{"Монако": "Стандарт"} или строка JSON → строка JSON только с нашими комнатами и пакетами."""
    if isinstance(v, str):
        try:
            v = json.loads(v or "{}")
        except ValueError:
            v = {}
    out = {r: p for r, p in (v or {}).items() if r in ROOMS and p in PACKAGES}
    return json.dumps(out, ensure_ascii=False) if out else ""


def _hours_param(hours: str, rooms: list[str]) -> dict[str, int]:
    """Параметр hours: «6» (всем комнатам) или «Монако:6,Сингапур:3» → минуты по комнатам."""
    own, default = {}, 3
    for part in str(hours or "").split(","):
        name, _, h = part.rpartition(":")
        if name:
            own[name.strip()] = _int(h) or 3
        elif part.strip():
            default = _int(part) or 3
    return {r: max(3, own.get(r, default)) * 60 for r in rooms}


def _int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(str(v).replace(" ", ""))
    except ValueError:
        raise HTTPException(422, f"Нужно число: {v}")


# ---------------------------------------------------------------- вывод


def user_out(u: User) -> dict:
    return {
        "id": u.id, "login": u.login, "name": u.name, "role": u.role, "phone": u.phone,
        "telegram": u.telegram, "color": u.color, "active": u.active,
    }


def lead_out(lead: Lead, at: datetime) -> dict:
    def d(v):
        return v.isoformat() if v else None

    return {
        "id": lead.id,
        "created_at": d(lead.created_at),
        "day_no": lead.day_no,
        "channel": lead.channel,
        "phone": format_phone(lead.phone_norm) or lead.phone,
        "phone_norm": lead.phone_norm,
        "created_by_id": lead.created_by_id,
        "name": lead.name,
        "source_id": lead.source_id,
        "source": f"{lead.source.group} · {lead.source.name}" if lead.source else "",
        "source_text": lead.source_text,
        "utm": {k: getattr(lead, "utm_" + k) for k in ("source", "medium", "campaign", "content", "term")},
        "holiday_date": d(lead.holiday_date),
        "holiday_text": lead.holiday_text,
        "holiday_dates": holiday_dates_of(lead),
        "admin_comment": lead.admin_comment,
        "manager_id": lead.manager_id,
        "manager": lead.manager.name if lead.manager else "",
        "late": lead.late,
        "taken_at": d(lead.taken_at),
        "status": lead.status,
        "refusal_reason": lead.refusal_reason,
        "contact_channel": lead.contact_channel,
        "child_age": lead.child_age,
        "kids": lead.kids,
        "adults": lead.adults,
        "package": lead.package,
        "next_contact_at": d(lead.next_contact_at),
        "manager_comment": lead.manager_comment,
        "review": lead.review,
        "booking_date": d(lead.booking_date),
        "booking_time": lead.booking_time,
        "booking_room": lead.booking_room,
        "booking_show": lead.booking_show,
        "booking_hours": lead.booking_hours or 3,
        "booking_room_hours": booking.room_hours(lead),
        "booking_room_packages": booking.room_packages(lead),
        "hold": booking.hold(lead, at),
        "prepaid_at": d(lead.prepaid_at),
        "prepaid_amount": lead.prepaid_amount,
        "updated_at": d(lead.updated_at),
        "flag": lead_flag(lead, at),
        "alert": is_alert(lead, at),
        "stale": is_stale(lead, at),
        "tags": [t for t in (lead.tags or "").split(",") if t],
        "merged_count": lead.merged_count or 0,
        "bumped_at": d(lead.bumped_at),
        # клиент написал ещё раз, а менеджер после этого заявку не трогал — поднять наверх
        "again": bool(lead.bumped_at and (lead.touched_at is None or lead.touched_at < lead.bumped_at)),
        "prev": _prev_info(lead),
    }


def _prev_info(lead: Lead) -> dict | None:
    """Прошлая заявка того же клиента (больше суток назад): кто вёл и чем кончилось."""
    if not lead.prev_lead_id:
        return None
    s = object_session(lead)
    p = s.get(Lead, lead.prev_lead_id) if s else None
    if not p:
        return None
    return {"id": p.id, "created_at": p.created_at.isoformat(), "manager": p.manager.name if p.manager else "",
            "status": p.status}


# поля, которые можно править в таблице, и как их разбирать
EDITABLE = {
    "name": _str, "phone": _str, "source_id": _int, "source_text": _str,
    "holiday_date": parse_date, "holiday_text": _str, "admin_comment": _str,
    "status": _str, "refusal_reason": _str, "contact_channel": _str,
    "child_age": _str, "kids": _str, "adults": _str, "package": _str,
    "next_contact_at": parse_dt, "manager_comment": _str, "review": _str,
    "booking_date": parse_date, "booking_time": _str, "booking_room": _str, "booking_show": _str, "tags": _str,
    "booking_hours": lambda v: max(3, min(12, _int(v) or 3)),
    "booking_room_hours": lambda v: _room_hours(v),
    "booking_room_packages": lambda v: _room_packages(v),
    "prepaid_at": parse_date, "prepaid_amount": _int,
    "manager_id": _int,
}
# правка этих полей = менеджер поработал с заявкой (снимает «Просрочено»)
WORK_FIELDS = set(EDITABLE) - {"manager_id"}


def holiday_dates_of(lead: Lead) -> list[str]:
    """Все даты праздника: несколько дат хранятся в holiday_text как «14.11.2026, 15.11.2026»."""
    out = []
    for part in (lead.holiday_text or "").split(","):
        d = parse_date(part.strip()) if part.strip() else None
        if not d:
            out = []
            break
        out.append(d.isoformat())
    if not out and lead.holiday_date:
        out = [lead.holiday_date.isoformat()]
    return sorted(set(out))


def expand_holiday_dates(data: dict) -> dict:
    """holiday_dates: [..] → holiday_date (самая ранняя) и holiday_text (все, если их несколько)."""
    if "holiday_dates" not in data:
        return data
    data = dict(data)
    raw = data.pop("holiday_dates") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    days = sorted({d for d in (parse_date(str(x).strip()) for x in raw if str(x).strip()) if d})
    if len(days) != len([x for x in raw if str(x).strip()]):
        raise HTTPException(422, "Не понял дату праздника")
    data["holiday_date"] = days[0].isoformat() if days else ""
    data["holiday_text"] = ", ".join(d.strftime("%d.%m.%Y") for d in days) if len(days) > 1 else ""
    return data


def apply_changes(s: Session, lead: Lead, data: dict, user: User | None, at: datetime) -> list[str]:
    changed = []
    data = expand_holiday_dates(data)
    for field, raw in data.items():
        if field not in EDITABLE:
            raise HTTPException(422, f"Поле нельзя менять: {field}")
        value = EDITABLE[field](raw)
        if field == "status" and value not in STATUSES:
            raise HTTPException(422, f"Нет такого статуса: {value}")
        # комментарии: у администратора и банкетного менеджера — каждый свою колонку, владелец — обе
        if user and field == "admin_comment" and user.role in MANAGER_ROLES:
            raise HTTPException(403, "Комментарий администратора заполняет администратор")
        if user and field == "manager_comment" and user.role == "admin":
            raise HTTPException(403, "Комментарий банкетного менеджера заполняет банкетный менеджер")
        if field == "manager_id":
            if user and user.role in MANAGER_ROLES:
                raise HTTPException(403, "Передать заявку другому менеджеру может администратор")
            if value is not None:
                m = s.get(User, value)
                if not m or not m.active:
                    raise HTTPException(422, "Менеджер не найден")
        if field == "source_id" and value is not None and not s.get(Source, value):
            raise HTTPException(422, "Источник не найден")
        if field == "phone":
            norm = normalize_phone(value)
            if value and not norm:
                raise HTTPException(422, "Не похоже на номер телефона")
            if norm != lead.phone_norm:
                s.add(LeadEvent(lead_id=lead.id, user_id=user.id if user else None, field="phone", old=lead.phone_norm, new=norm, at=at))
                changed.append("phone")
            lead.phone, lead.phone_norm = value, norm
            continue
        old = getattr(lead, field)
        if old == value:
            continue
        setattr(lead, field, value)
        s.add(LeadEvent(lead_id=lead.id, user_id=user.id if user else None, field=field,
                        old="" if old is None else str(old), new="" if value is None else str(value), at=at))
        changed.append(field)
    if set(changed) & WORK_FIELDS:
        lead.touched_at = at
        if lead.taken_at is None and user and user.id == lead.manager_id:
            lead.taken_at = at
    if "status" in changed and lead.status != "отказ":
        lead.refusal_reason = ""
    # отказ, нецелевой, удалена — время в журнале освобождается, бронь — в «Удалённые»
    if "status" in changed and lead.status in CLOSED_STATUSES - {DELETED_STATUS} and release_booking(s, lead, user, at):
        changed.append("booking_released")
    # бронь поставили или перенесли — с этого момента сутки на предоплату
    if set(changed) & {"booking_date", "booking_time", "booking_room"}:
        lead.booking_at = at if (lead.booking_date and lead.booking_time and lead.booking_room) else None
    return changed


def create_lead(s: Session, data: dict, user: User | None, channel: str, at: datetime) -> tuple[Lead, dict]:
    phone = _str(data.get("phone"))
    norm = normalize_phone(phone)
    if not norm:
        raise HTTPException(422, "Нужен телефон клиента")
    manager_id = _int(data.get("manager_id"))
    if manager_id is not None:
        m = s.get(User, manager_id)
        if not m or not m.active:
            raise HTTPException(422, "Менеджер не найден")
        res = {"manager_id": manager_id, "late": False, "reason": "manual"}
        late = not any(sh.manager_id == manager_id for sh in shifts_now(s, at))
    else:
        a = pick_manager(s, at, norm)
        manager_id, late = a.manager_id, a.late
        res = {"manager_id": a.manager_id, "late": a.late, "reason": a.reason}
    lead = Lead(
        created_at=at, channel=channel, created_by_id=user.id if user else None,
        phone=phone, phone_norm=norm, manager_id=manager_id, late=late,
        day_no=next_day_no(s, manager_id, at),
    )
    s.add(lead)
    s.flush()
    fields = {k: v for k, v in expand_holiday_dates(data).items() if k in EDITABLE and k not in ("phone", "manager_id", "status")}
    apply_changes(s, lead, fields, None, at)
    prev = s.scalars(select(Lead).where(Lead.phone_norm == norm, Lead.id != lead.id, Lead.status != DELETED_STATUS)
                     .order_by(Lead.created_at.desc())).first()
    if prev:
        lead.prev_lead_id = prev.id
        for f in PREFILL_FROM_PREV:          # что клиент уже рассказывал — не спрашиваем заново
            if not getattr(lead, f) and getattr(prev, f):
                setattr(lead, f, getattr(prev, f))
    lead.touched_at = None
    for k in ("source", "medium", "campaign", "content", "term"):
        setattr(lead, "utm_" + k, _str(data.get("utm_" + k))[:250])
    s.add(LeadEvent(lead_id=lead.id, user_id=user.id if user else None, field="created",
                    new=f"{channel}; менеджер {manager_id or '—'} ({res['reason']})", at=at))
    s.flush()
    return lead, res


PREFILL_FROM_PREV = ("name", "child_age", "kids", "adults", "package", "contact_channel")
MERGE_WINDOW = timedelta(hours=24)
EXCURSION_TAG = "экскурсия"


def has_excursion(*texts: str) -> bool:
    return any("экскурс" in (t or "").lower() for t in texts)


def add_tag(lead: Lead, tag: str) -> None:
    tags = [t for t in (lead.tags or "").split(",") if t]
    if tag not in tags:
        lead.tags = ",".join(tags + [tag])


def find_recent(s: Session, norm: str, at: datetime) -> Lead | None:
    """Заявка того же клиента за последние 24 часа (считая от последней повторной) — в неё вливаем новую."""
    last = s.scalars(select(Lead).where(Lead.phone_norm == norm, Lead.status != DELETED_STATUS)
                     .order_by(Lead.created_at.desc())).first()
    if last and max(last.created_at, last.bumped_at or last.created_at) >= at - MERGE_WINDOW:
        return last
    return None


def merge_into(s: Session, lead: Lead, payload: dict, channel: str, at: datetime) -> None:
    """Повторная заявка клиента: пустые поля дополняем, в комментарий — строка о новой заявке,
    заявка поднимается наверх у своего менеджера (bumped_at)."""
    fields = {k: v for k, v in expand_holiday_dates(payload).items() if k in EDITABLE and k not in ("phone", "manager_id", "status")}
    for k, v in fields.items():
        if k == "admin_comment":
            continue
        cur = getattr(lead, k)
        if (cur is None or cur == "") and v not in (None, "", []):
            setattr(lead, k, EDITABLE[k](v) if k not in ("holiday_dates",) else v)
    lines = [x for x in (payload.get("source_text"), payload.get("admin_comment")) if x]
    lead.admin_comment = ((lead.admin_comment or "").rstrip() + f"\n+ ещё заявка {at:%d.%m %H:%M}"
                          + (": " + " · ".join(lines) if lines else "")).strip()
    lead.merged_count = (lead.merged_count or 0) + 1
    lead.bumped_at = at
    s.add(LeadEvent(lead_id=lead.id, field="merged", new=json.dumps({"channel": channel, "form": payload.get("source_text", ""),
                    "comment": payload.get("admin_comment", "")[:500]}, ensure_ascii=False), at=at))


def duplicates(s: Session, norm: str, exclude: int | None = None) -> list[dict]:
    if not norm:
        return []
    q = select(Lead).where(Lead.phone_norm == norm, Lead.status != DELETED_STATUS).order_by(Lead.created_at.desc()).limit(5)
    if exclude:
        q = q.where(Lead.id != exclude)
    return [
        {"id": l.id, "created_at": l.created_at.isoformat(), "manager": l.manager.name if l.manager else "",
         "manager_id": l.manager_id, "status": l.status, "name": l.name}
        for l in s.scalars(q)
    ]


# ---------------------------------------------------------------- маршруты


def register(app: FastAPI) -> None:
    @app.get("/")
    def index():
        # к style.css и app.js дописываем версию (время изменения файла): после обновления
        # панели браузер сразу берёт новые файлы, а не старые из кэша
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for f in ("style.css", "sounds.js", "app.js"):
            html = html.replace(f"/static/{f}\"", f"/static/{f}?v={int((STATIC / f).stat().st_mtime)}\"")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # --- вход

    @app.post("/api/login")
    def login(body: dict, response: Response, s: Session = Depends(db)):
        user = s.scalar(select(User).where(User.login == _str(body.get("login")).lower()))
        if not user or not user.active or not auth.check_password(_str(body.get("password")), user.password_hash):
            raise HTTPException(401, "Неверный логин или пароль")
        response.set_cookie(auth.COOKIE, auth.make_token(user.id, user.password_hash), httponly=True,
                            samesite="lax", secure=get_settings().cookie_secure, max_age=auth.SESSION_DAYS * 86400)
        return user_out(user)

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return user_out(user)

    @app.post("/api/me/password")
    def change_password(body: dict, response: Response, user: User = Depends(current_user), s: Session = Depends(db)):
        if not auth.check_password(_str(body.get("old")), user.password_hash):
            raise HTTPException(422, "Старый пароль не подходит")
        new = _str(body.get("new"))
        if len(new) < 6:
            raise HTTPException(422, "Пароль от 6 символов")
        u = s.get(User, user.id)
        u.password_hash = auth.hash_password(new)
        response.set_cookie(auth.COOKIE, auth.make_token(u.id, u.password_hash), httponly=True, samesite="lax",
                            secure=get_settings().cookie_secure, max_age=auth.SESSION_DAYS * 86400)
        return {"ok": True}

    # --- справочники

    @app.get("/api/meta")
    def meta(user: User = Depends(current_user), s: Session = Depends(db)):
        at = now()
        sources = s.scalars(select(Source).order_by(Source.order, Source.id)).all()
        managers = s.scalars(select(User).where(User.role.in_(MANAGER_ROLES)).order_by(User.name)).all()
        on_shift = shifts_now(s, at)
        return {
            "now": at.isoformat(),
            "statuses": STATUSES, "packages": PACKAGES, "refusal_reasons": REFUSAL_REASONS,
            "contact_channels": CONTACT_CHANNELS, "flags": FLAGS, "flag_order": FLAG_ORDER,
            "booking_times": [{"group": g, "times": t} for g, t in BOOKING_TIMES],
            "rooms": ROOMS, "shows": SHOWS, "prepaid_presets": PREPAID_PRESETS, "prices": PRICES, "sites": SITE_LABELS, "site_urls": SITE_URLS,
            "sources": [{"id": x.id, "group": x.group, "name": x.name, "active": x.active} for x in sources],
            "managers": [{"id": m.id, "name": m.name, "color": m.color, "active": m.active} for m in managers],
            "on_shift": [{"manager_id": sh.manager_id, "name": sh.manager.name} for sh in on_shift],
            "shift_change": f"{get_settings().shift_change_hour:02d}:00",
        }

    # --- заявки

    @app.get("/api/leads")
    def list_leads(
        request: Request,
        manager_id: int | None = None,
        flag: str | None = None,
        status: str | None = None,
        q: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 500,
        user: User = Depends(current_user),
        s: Session = Depends(db),
    ):
        at = now()
        query = select(Lead).order_by(Lead.created_at.desc())
        if user.role in MANAGER_ROLES:
            query = query.where(Lead.manager_id == user.id)
        elif manager_id:
            query = query.where(Lead.manager_id == manager_id)
        if status:
            query = query.where(Lead.status == status)
        else:
            query = query.where(Lead.status != DELETED_STATUS)   # удалённые — только в фильтре «Удалённые»
        if q:
            like = f"%{q.strip()}%"
            digits = normalize_phone(q) or "".join(ch for ch in q if ch.isdigit())
            conds = [Lead.name.ilike(like), Lead.admin_comment.ilike(like), Lead.manager_comment.ilike(like)]
            if len(digits) >= 4:
                conds.append(Lead.phone_norm.contains(digits[-10:]))
            query = query.where(or_(*conds))
        df, dt = parse_date(date_from), parse_date(date_to)
        if df:
            query = query.where(Lead.created_at >= datetime.combine(df, datetime.min.time()))
        if dt:
            query = query.where(Lead.created_at < datetime.combine(dt + timedelta(days=1), datetime.min.time()))
        leads = list(s.scalars(query).all())
        # «Жёлтые» заявки поднимаются в смену своего менеджера: в день его смены они видны
        # в таблице группой «Перенесено на смену», даже если фильтр дат их не захватывает.
        today = shift_day(at)
        on_shift = set(s.scalars(select(Shift.manager_id).where(Shift.day == today)))
        carry_q = select(Lead).where(Lead.status.in_(WAITING_STATUSES), Lead.manager_id.in_(on_shift))
        if user.role in MANAGER_ROLES:
            carry_q = carry_q.where(Lead.manager_id == user.id)
        elif manager_id:
            carry_q = carry_q.where(Lead.manager_id == manager_id)
        if not (status or q) and on_shift:
            seen = {l.id for l in leads}
            leads += [l for l in s.scalars(carry_q) if l.id not in seen]
        rows = []
        for l in leads:
            r = lead_out(l, at)
            r["carry"] = l.status in WAITING_STATUSES and l.manager_id in on_shift and shift_day(l.created_at) < today
            # мигает только в день смены своего менеджера
            r["stale"] = r["stale"] and l.manager_id in on_shift
            rows.append(r)
        counters = Counter(r["flag"] for r in rows if r["flag"])
        if flag:
            rows = [r for r in rows if r["flag"] == flag]
        return {"now": at.isoformat(), "counters": {f: counters.get(f, 0) for f in FLAG_ORDER},
                "total": len(rows), "leads": rows[:limit]}

    @app.get("/api/leads/check-phone")
    def check_phone(phone: str, user: User = Depends(current_user), s: Session = Depends(db)):
        norm = normalize_phone(phone)
        at = now()
        a = pick_manager(s, at, norm)
        mgr = s.get(User, a.manager_id) if a.manager_id else None
        recent = find_recent(s, norm, at) if norm else None
        return {"phone_norm": norm, "valid": bool(norm), "formatted": format_phone(norm),
                "duplicates": duplicates(s, norm),
                "merge_into": {"id": recent.id, "day_no": recent.day_no, "created_at": recent.created_at.isoformat(),
                               "manager": recent.manager.name if recent.manager else ""} if recent else None,
                "suggested": {"manager_id": a.manager_id, "manager": mgr.name if mgr else "", "late": a.late, "reason": a.reason}}

    @app.post("/api/leads")
    def new_lead(body: dict, user: User = Depends(current_user), s: Session = Depends(db)):
        at = now()
        # тот же клиент за последние 24 часа — дополняем его заявку, новую строку не заводим
        recent = find_recent(s, normalize_phone(_str(body.get("phone"))), at)
        if recent and can_see(user, recent):
            merge_into(s, recent, body, "manual", at)
            if has_excursion(_str(body.get("admin_comment"))):
                add_tag(recent, EXCURSION_TAG)
            s.flush()
            tg_booking.sync(recent)
            return {"lead": lead_out(recent, at), "merged": True,
                    "assignment": {"manager_id": recent.manager_id, "late": recent.late, "reason": "merged"}}
        lead, res = create_lead(s, body, user, "manual", at)
        if has_excursion(_str(body.get("admin_comment"))):
            add_tag(lead, EXCURSION_TAG)
        tg_booking.sync(lead)
        return {"lead": lead_out(lead, at), "assignment": res}

    @app.get("/api/leads/{lead_id}")
    def get_lead(lead_id: int, user: User = Depends(current_user), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or not can_see(user, lead):
            raise HTTPException(404, "Заявка не найдена")
        at = now()
        events = s.scalars(select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.at, LeadEvent.id)).all()
        return {"lead": lead_out(lead, at), "duplicates": duplicates(s, lead.phone_norm, exclude=lead.id),
                "history": [{"at": e.at.isoformat(), "user": e.user.name if e.user else "система", "field": e.field,
                             "old": e.old, "new": e.new} for e in events]}

    @app.patch("/api/leads/{lead_id}")
    def patch_lead(lead_id: int, body: dict, user: User = Depends(current_user), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or not can_see(user, lead):
            raise HTTPException(404, "Заявка не найдена")
        if not can_manage(user, lead) and set(body) - {"admin_comment"}:
            raise HTTPException(403, "В чужой заявке администратор пишет только свой комментарий")
        at = now()
        changed = apply_changes(s, lead, body, user, at)
        if "manager_id" in changed:
            lead.taken_at = None
            lead.late = False
        tg_booking.sync(lead)
        s.flush()
        return {"lead": lead_out(lead, at), "changed": changed}

    # --- удаление заявки (тестовой, дубля): статус «удалена», прежний статус — в истории; можно вернуть

    @app.post("/api/leads/{lead_id}/delete")
    def delete_lead(lead_id: int, user: User = Depends(owner), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404, "Заявка не найдена")
        if lead.status == DELETED_STATUS:
            raise HTTPException(422, "Заявка уже удалена")
        at = now()
        s.add(LeadEvent(lead_id=lead.id, user_id=user.id, field="lead_deleted", old=lead.status, new=DELETED_STATUS, at=at))
        lead.status = DELETED_STATUS
        s.flush()
        tg_booking.sync(lead)
        return {"lead": lead_out(lead, at)}

    @app.post("/api/leads/{lead_id}/restore")
    def restore_lead(lead_id: int, user: User = Depends(owner), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or lead.status != DELETED_STATUS:
            raise HTTPException(404, "Удалённая заявка не найдена")
        ev = s.scalars(select(LeadEvent).where(LeadEvent.lead_id == lead.id, LeadEvent.field == "lead_deleted")
                       .order_by(LeadEvent.at.desc(), LeadEvent.id.desc())).first()
        at = now()
        lead.status = ev.old if ev and ev.old in STATUSES else "новая"
        s.add(LeadEvent(lead_id=lead.id, user_id=user.id, field="lead_restored", old=DELETED_STATUS, new=lead.status, at=at))
        s.flush()
        tg_booking.sync(lead)
        return {"lead": lead_out(lead, at)}

    # --- удаление брони с возможностью вернуть
    # Бронь (дата, время, комната) снимается с заявки, а её прежние значения пишутся в историю
    # событием booking_deleted. Возврат — событие booking_restored со ссылкой на удаление.

    BOOKING_SLOT = ("booking_date", "booking_time", "booking_room", "booking_hours", "booking_room_hours", "booking_room_packages")

    @app.post("/api/leads/{lead_id}/booking/delete")
    def delete_booking(lead_id: int, user: User = Depends(staff), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or not can_see(user, lead):
            raise HTTPException(404, "Заявка не найдена")
        if not can_manage(user, lead):
            raise HTTPException(403, "Чужую бронь может снять только её менеджер")
        if not (lead.booking_date or lead.booking_time or lead.booking_room):
            raise HTTPException(422, "У заявки нет брони")
        at = now()
        release_booking(s, lead, user, at)
        lead.touched_at = at
        s.flush()
        tg_booking.sync(lead)
        return {"lead": lead_out(lead, at)}

    def _deleted_events(s: Session, user: User) -> list[LeadEvent]:
        restored = {e.new for e in s.scalars(select(LeadEvent).where(LeadEvent.field == "booking_restored"))}
        q = select(LeadEvent).where(LeadEvent.field == "booking_deleted").order_by(LeadEvent.at.desc(), LeadEvent.id.desc())
        out = []
        for e in s.scalars(q.limit(500)):
            lead = s.get(Lead, e.lead_id)
            if str(e.id) not in restored and lead and can_see(user, lead):
                out.append(e)
        return out

    @app.get("/api/bookings/deleted")
    def deleted_bookings(user: User = Depends(staff), s: Session = Depends(db)):
        out = []
        for e in _deleted_events(s, user)[:200]:
            lead, b = s.get(Lead, e.lead_id), json.loads(e.old or "{}")
            out.append({"event_id": e.id, "lead_id": lead.id, "deleted_at": e.at.isoformat(),
                        "deleted_by": e.user.name if e.user else "", **b, "name": lead.name,
                        "phone": format_phone(lead.phone_norm) or lead.phone, "package": lead.package,
                        "manager": lead.manager.name if lead.manager else "", "status": lead.status,
                        "has_booking": bool(lead.booking_date)})
        return out

    @app.post("/api/bookings/deleted/{event_id}/restore")
    def restore_booking(event_id: int, force: bool = False, user: User = Depends(staff), s: Session = Depends(db)):
        e = s.get(LeadEvent, event_id)
        if not e or e.field != "booking_deleted" or e not in _deleted_events(s, user):
            raise HTTPException(404, "Удалённая бронь не найдена")
        lead, b = s.get(Lead, e.lead_id), json.loads(e.old or "{}")
        if not can_manage(user, lead):
            raise HTTPException(403, "Чужую бронь может вернуть только её менеджер")
        if lead.booking_date:
            raise HTTPException(409, "У заявки уже стоит другая бронь — сначала удалите её")
        day, start = parse_date(b.get("booking_date")), booking.parse_hm(b.get("booking_time", ""))
        if day and start is not None and booking.rooms_of(b.get("booking_room")):
            rooms = booking.rooms_of(b["booking_room"])
            mins = {r: h * 60 for r, h in json.loads(_room_hours(b.get("booking_room_hours")) or "{}").items()}
            warns = booking.check_rooms(booking.day_bookings(s, day, lead.id), day, rooms, start,
                                        {r: mins.get(r, (b.get("booking_hours") or 3) * 60) for r in rooms})
            if any(w["level"] == "red" for w in warns) and not force:
                return JSONResponse({"conflict": True, "warnings": warns}, status_code=409)
        at = now()
        apply_changes(s, lead, {k: b.get(k, 3 if k == "booking_hours" else "") for k in BOOKING_SLOT}, user, at)
        s.add(LeadEvent(lead_id=lead.id, user_id=user.id, field="booking_restored", new=str(e.id), at=at))
        s.flush()
        tg_booking.sync(lead)
        return {"lead": lead_out(lead, at)}

    @app.post("/api/leads/{lead_id}/take")
    def take_lead(lead_id: int, user: User = Depends(current_user), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or not can_see(user, lead):
            raise HTTPException(404, "Заявка не найдена")
        at = now()
        if lead.taken_at is None and can_manage(user, lead):
            lead.taken_at = at
            lead.touched_at = at
            if lead.manager_id is None and user.role in MANAGER_ROLES:
                lead.manager_id = user.id
            s.add(LeadEvent(lead_id=lead.id, user_id=user.id, field="taken", new=at.isoformat(), at=at))
        return {"lead": lead_out(lead, at)}

    @app.get("/api/notifications")
    def notifications(since: str, user: User = Depends(current_user), s: Session = Depends(db)):
        """Для уведомлений в браузере: новые заявки и подошедшие контакты с момента since."""
        at = now()
        since_dt = parse_dt(since) or at - timedelta(minutes=1)
        mine = Lead.manager_id == user.id
        new_q = select(Lead).where(Lead.created_at > since_dt, Lead.created_at <= at)
        again_q = select(Lead).where(Lead.bumped_at > since_dt, Lead.bumped_at <= at)
        waiting_q = select(Lead).where(Lead.status == "новая", Lead.taken_at.is_(None),
                                       Lead.created_at <= at - timedelta(minutes=get_settings().new_lead_alert_minutes))
        due_q = select(Lead).where(Lead.next_contact_at > since_dt, Lead.next_contact_at <= at)
        if user.role in MANAGER_ROLES:
            new_q, due_q, again_q, waiting_q = new_q.where(mine), due_q.where(mine), again_q.where(mine), waiting_q.where(mine)
        else:
            due_q = due_q.where(mine)
        return {"now": at.isoformat(),
                "new": [lead_out(l, at) for l in s.scalars(new_q)],
                "due": [lead_out(l, at) for l in s.scalars(due_q)],
                "again": [lead_out(l, at) for l in s.scalars(again_q)],
                # новые заявки, которые не взяли дольше порога: у менеджера звучит сирена, пока не возьмёт
                "waiting": [{"id": l.id, "name": l.name, "phone": format_phone(l.phone_norm) or l.phone,
                             "manager": l.manager.name if l.manager else "",
                             "minutes": int((at - l.created_at).total_seconds() // 60)}
                            for l in s.scalars(waiting_q)]}

    # --- журнал записи (брони комнат)

    def _booking_out(l: Lead, user: User, room: str) -> dict:
        start, minutes = booking.parse_hm(l.booking_time), booking.room_hours(l).get(room, 3) * 60
        out = {"id": l.id, "room": room, "rooms": booking.rooms_of(l.booking_room), "minutes": minutes,
               "start": booking.hm(start), "end": booking.hm(start + minutes),
               "package": booking.room_packages(l).get(room) or l.package, "status": l.status, "manager": l.manager.name if l.manager else "",
               "paid": booking.is_paid(l), "hold": booking.hold(l), "own": can_see(user, l)}
        if out["own"]:  # чужие брони менеджер видит только как «занято»
            out.update({"name": l.name, "phone": format_phone(l.phone_norm) or l.phone, "child_age": l.child_age,
                        "kids": l.kids, "adults": l.adults, "show": l.booking_show, "prepaid_amount": l.prepaid_amount})
        return out

    @app.get("/api/journal")
    def journal(date: str, exclude_id: int | None = None, user: User = Depends(current_user), s: Session = Depends(db)):
        """exclude_id — заявка, бронь которой сейчас выбирают в журнале: её сохранённую бронь не показываем."""
        day = parse_date(date)
        leads = sorted((l for l in booking.day_leads(s, day) if l.id != exclude_id), key=lambda l: booking.parse_hm(l.booking_time))
        bookings = booking.day_bookings(s, day, exclude_id)
        return {"date": day.isoformat(), "weekend": day.weekday() in booking.WEEKEND, "rooms": ROOMS,
                "bookings": [_booking_out(l, user, r) for l in leads for r in booking.rooms_of(l.booking_room)],
                "slots": {r: booking.slots(bookings, day, r) for r in ROOMS},
                "party_minutes": booking.PARTY, "cleaning_minutes": booking.CLEANING, "gap_minutes": booking.ROOM_GAP}

    @app.get("/api/journal/month")
    def journal_month(month: str, user: User = Depends(current_user), s: Session = Depends(db)):
        """Сколько броней в каждый день месяца — точки в календаре журнала."""
        first = parse_date(month + "-01")
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        q = select(Lead.booking_date, Lead.booking_time).where(Lead.booking_date >= first, Lead.booking_date < last,
                                                              Lead.booking_room != "", Lead.status.not_in(CLOSED_STATUSES))
        days = Counter(d.isoformat() for d, t in s.execute(q) if booking.parse_hm(t) is not None)
        return {"month": first.strftime("%Y-%m"), "days": dict(days)}

    @app.get("/api/bookings/check")
    def booking_check(date: str, room: str, time: str, exclude_id: int | None = None, hours: str = "3",
                      user: User = Depends(current_user), s: Session = Depends(db)):
        """Предупреждения для брони: комната занята, мало перерыва, старты близко, ломает сетку выходного.
        room — одна комната или несколько через «+»."""
        day, start, rooms = parse_date(date), booking.parse_hm(time), booking.rooms_of(room)
        if not day or not rooms or start is None:
            return {"warnings": []}
        return {"warnings": booking.check_rooms(booking.day_bookings(s, day, exclude_id), day, rooms, start, _hours_param(hours, rooms))}

    @app.get("/api/bookings/slots")
    def booking_slots(date: str, room: str, exclude_id: int | None = None, hours: int = 3,
                      user: User = Depends(current_user), s: Session = Depends(db)):
        day = parse_date(date)
        if not day or room not in ROOMS:
            return {"slots": []}
        return {"slots": booking.slots(booking.day_bookings(s, day, exclude_id), day, room, max(3, hours) * 60)}

    # --- менеджеры и пользователи

    @app.get("/api/users")
    def list_users(user: User = Depends(owner), s: Session = Depends(db)):
        return [user_out(u) for u in s.scalars(select(User).order_by(User.role, User.name))]

    @app.post("/api/users")
    def add_user(body: dict, user: User = Depends(owner), s: Session = Depends(db)):
        login_ = _str(body.get("login")).lower()
        role = _str(body.get("role")) or "manager"
        if role not in ROLES:
            raise HTTPException(422, "Роль: owner, admin или manager")
        if role in TOP_ROLES and user.role not in TOP_ROLES:
            raise HTTPException(403, "Владельца может добавить только владелец")
        if not login_ or not _str(body.get("name")):
            raise HTTPException(422, "Нужны логин и имя")
        if len(_str(body.get("password"))) < 6:
            raise HTTPException(422, "Пароль от 6 символов")
        if s.scalar(select(User).where(User.login == login_)):
            raise HTTPException(422, "Такой логин уже есть")
        u = User(login=login_, name=_str(body["name"]), role=role, password_hash=auth.hash_password(_str(body["password"])),
                 phone=_str(body.get("phone")), telegram=_str(body.get("telegram")), color=_str(body.get("color")) or "#4f7cff")
        s.add(u)
        s.flush()
        return user_out(u)

    @app.patch("/api/users/{user_id}")
    def edit_user(user_id: int, body: dict, user: User = Depends(owner), s: Session = Depends(db)):
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Не найден")
        if u.role in TOP_ROLES and user.role not in TOP_ROLES:
            raise HTTPException(403, "Владельца может менять только владелец")
        for f in ("name", "phone", "telegram", "color"):
            if f in body:
                setattr(u, f, _str(body[f]))
        if "active" in body:
            if u.id == user.id and not body["active"]:
                raise HTTPException(422, "Нельзя отключить себя")
            u.active = bool(body["active"])
        if "role" in body:
            role = _str(body["role"])
            # Роли у существующих сотрудников меняет только разработчик
            if user.role != "owner":
                raise HTTPException(403, "Роль может менять только разработчик")
            if u.id == user.id and role != u.role:
                raise HTTPException(422, "Свою роль поменять нельзя")
            if role not in ROLES:
                raise HTTPException(422, "Нельзя поставить такую роль")
            u.role = role
        if _str(body.get("password")):
            if len(_str(body["password"])) < 6:
                raise HTTPException(422, "Пароль от 6 символов")
            u.password_hash = auth.hash_password(_str(body["password"]))
        return user_out(u)

    # --- расписание

    @app.get("/api/shifts")
    def list_shifts(month: str | None = None, user: User = Depends(current_user), s: Session = Depends(db)):
        at = now()
        first = parse_date((month or at.strftime("%Y-%m")) + "-01")
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        rows = s.scalars(select(Shift).where(Shift.day >= first, Shift.day < last).order_by(Shift.day, Shift.start)).all()
        return [{"id": r.id, "day": r.day.isoformat(), "manager_id": r.manager_id, "manager": r.manager.name,
                 "color": r.manager.color, "start": r.start, "end": r.end} for r in rows]

    @app.put("/api/shifts")
    def put_shift(body: dict, user: User = Depends(scheduler), s: Session = Depends(db)):
        day = parse_date(body.get("day"))
        mid = _int(body.get("manager_id"))
        m = s.get(User, mid) if mid else None
        if not day or not m or m.role not in MANAGER_ROLES:
            raise HTTPException(422, "Нужны день и менеджер")
        sh = s.scalar(select(Shift).where(Shift.day == day, Shift.manager_id == mid))
        if not sh:
            sh = Shift(day=day, manager_id=mid)
            s.add(sh)
        s.flush()
        return {"id": sh.id}

    @app.post("/api/shifts/bulk")
    def bulk_shifts(body: dict, user: User = Depends(scheduler), s: Session = Depends(db)):
        """Поставить (on=true) или снять (on=false) менеджера сразу на несколько дней."""
        mid = _int(body.get("manager_id"))
        m = s.get(User, mid) if mid else None
        days = [parse_date(d) for d in (body.get("days") or [])]
        if not m or m.role not in MANAGER_ROLES or not days or None in days or len(days) > 62:
            raise HTTPException(422, "Нужны менеджер и дни")
        on = bool(body.get("on", True))
        changed = 0
        for day in sorted(set(days)):
            sh = s.scalar(select(Shift).where(Shift.day == day, Shift.manager_id == mid))
            if on and not sh:
                s.add(Shift(day=day, manager_id=mid))
                changed += 1
            elif not on and sh:
                s.delete(sh)
                changed += 1
        return {"changed": changed}

    @app.delete("/api/shifts/{shift_id}")
    def delete_shift(shift_id: int, user: User = Depends(scheduler), s: Session = Depends(db)):
        sh = s.get(Shift, shift_id)
        if sh:
            s.delete(sh)
        return {"ok": True}

    @app.post("/api/shifts/copy-week")
    def copy_week(body: dict, user: User = Depends(scheduler), s: Session = Depends(db)):
        """Скопировать смены недели (с понедельника src) на неделю с понедельника dst. Занятые дни не трогает."""
        src, dst = parse_date(body.get("src")), parse_date(body.get("dst"))
        if not src or not dst:
            raise HTTPException(422, "Нужны две даты")
        shift_days = (dst - src).days
        made = 0
        for sh in s.scalars(select(Shift).where(Shift.day >= src, Shift.day < src + timedelta(days=7))).all():
            day = sh.day + timedelta(days=shift_days)
            if not s.scalar(select(Shift).where(Shift.day == day, Shift.manager_id == sh.manager_id)):
                s.add(Shift(day=day, manager_id=sh.manager_id))
                made += 1
        return {"created": made}

    # --- источники «Как узнали»

    @app.post("/api/sources")
    def add_source(body: dict, user: User = Depends(owner), s: Session = Depends(db)):
        group, name = _str(body.get("group")), _str(body.get("name"))
        if not group or not name:
            raise HTTPException(422, "Нужны группа и название")
        order = (s.scalar(select(func.max(Source.order))) or 0) + 1
        src = Source(group=group, name=name, order=order)
        s.add(src)
        s.flush()
        return {"id": src.id}

    @app.patch("/api/sources/{source_id}")
    def edit_source(source_id: int, body: dict, user: User = Depends(owner), s: Session = Depends(db)):
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(404, "Не найден")
        for f in ("group", "name"):
            if f in body:
                setattr(src, f, _str(body[f]))
        if "active" in body:
            src.active = bool(body["active"])
        return {"ok": True}

    # --- сводка для руководителя

    @app.get("/api/manager-stats")
    def manager_stats_ep(month: str | None = None, user: User = Depends(owner), s: Session = Depends(db)):
        """Итоги банкетных менеджеров за месяц (только владелец) и прошлый месяц для сравнения."""
        today = now().date()
        m = parse_date(month + "-01") if month and len(month) == 7 else None
        m = (m or today).replace(day=1)
        return {"cur": month_stats(s, m, today), "prev": month_stats(s, prev_month(m), today)}

    @app.get("/api/stats")
    def stats(date_from: str | None = None, date_to: str | None = None, user: User = Depends(staff), s: Session = Depends(db)):
        at = now()
        df = parse_date(date_from) or (at.date() - timedelta(days=30))
        dt = parse_date(date_to) or at.date()
        leads = s.scalars(select(Lead).where(Lead.created_at >= datetime.combine(df, datetime.min.time()),
                                             Lead.created_at < datetime.combine(dt + timedelta(days=1), datetime.min.time()),
                                             Lead.status != DELETED_STATUS)).all()

        def block(items):
            n = len(items)
            booked = sum(1 for l in items if l.status == "бронь оплачена")
            return {"leads": n, "booked": booked, "conversion": round(booked / n * 100, 1) if n else 0,
                    "refused": sum(1 for l in items if l.status == "отказ"),
                    "untaken": sum(1 for l in items if l.taken_at is None and l.status == "новая"),
                    "prepaid_sum": sum(l.prepaid_amount or 0 for l in items)}

        by_manager, by_source = {}, {}
        for l in leads:
            by_manager.setdefault(l.manager.name if l.manager else "без менеджера", []).append(l)
            by_source.setdefault(f"{l.source.group} · {l.source.name}" if l.source else (l.utm_source or "не указан"), []).append(l)
        return {"from": df.isoformat(), "to": dt.isoformat(), "total": block(leads),
                "managers": {k: block(v) for k, v in sorted(by_manager.items())},
                "sources": {k: block(v) for k, v in sorted(by_source.items(), key=lambda kv: -len(kv[1]))},
                "refusal_reasons": dict(Counter(l.refusal_reason or "не указана" for l in leads if l.status == "отказ"))}

    # --- приём заявок с сайтов

    @app.post("/api/intake")
    async def intake(request: Request, s: Session = Depends(db)):
        """Заявка с сайта. Тильда шлёт form-urlencoded, свои сайты и консоль могут слать JSON.

        Адрес: /api/intake?token=…&site=tilda-landing
        """
        token = request.query_params.get("token") or request.headers.get("x-intake-token", "")
        cfg = get_settings()
        if not cfg.intake_token or token != cfg.intake_token:
            raise HTTPException(403, "Неверный токен")
        if "json" in request.headers.get("content-type", ""):
            raw = await request.json()
        else:
            raw = dict((await request.form()).items())
        # Тильда при подключении вебхука присылает test=test
        if raw.get("test") == "test" and len(raw) <= 2:
            return {"ok": True, "test": True}
        data = {k.lower(): v for k, v in raw.items()}
        phone = data.get("phone") or data.get("телефон") or data.get("tel") or ""
        if not normalize_phone(str(phone)):
            return JSONResponse({"ok": False, "error": "нет телефона"}, status_code=422)
        comment_parts = [str(data[k]) for k in ("comment", "comments", "message", "комментарий", "textarea") if data.get(k)]
        site = request.query_params.get("site") or _str(data.get("site")) or "site"
        payload = {
            "phone": phone,
            "name": _str(data.get("name") or data.get("имя")),
            "admin_comment": "\n".join(comment_parts),
            "source_text": _str(data.get("formname") or data.get("form")),   # название формы; сайт виден по channel
            **{"utm_" + k: _str(data.get("utm_" + k)) for k in ("source", "medium", "campaign", "content", "term")},
        }
        # поля формы своего сайта: дата, возраст, дети, пакет, способ связи (необязательные)
        payload.update(_intake_extras(data))
        src_name = _str(data.get("source_name"))
        if src_name:
            src = s.scalar(select(Source).where(Source.name == src_name))
            if src:
                payload["source_id"] = src.id
        if "source_id" not in payload:
            src = s.scalar(select(Source).where(Source.name == _source_by_utm(payload)))
            if src:
                payload["source_id"] = src.id
        at = now()
        excursion = has_excursion(payload.get("admin_comment", ""), payload.get("source_text", ""), _str(data.get("event")))
        # тот же клиент за последние 24 часа — не новая строка, а дополнение прежней заявки
        recent = find_recent(s, normalize_phone(str(phone)), at)
        if recent:
            merge_into(s, recent, payload, site[:32], at)
            if excursion:
                add_tag(recent, EXCURSION_TAG)
            s.flush()
            return {"ok": True, "id": recent.id, "merged": True}
        lead, res = create_lead(s, payload, None, site[:32], at)
        if excursion:
            add_tag(lead, EXCURSION_TAG)
        return {"ok": True, "id": lead.id, "assignment": res}

    # --- выгрузка для консоли

    @app.get("/api/export/leads")
    def export_leads(request: Request, updated_since: str | None = None, created_from: str | None = None,
                     created_to: str | None = None, s: Session = Depends(db)):
        """Заявки для консоли: изменённые после updated_since или созданные в днях created_from…created_to."""
        token = request.query_params.get("token") or request.headers.get("x-export-token", "")
        cfg = get_settings()
        if not cfg.export_token or token != cfg.export_token:
            raise HTTPException(403, "Неверный токен")
        at = now()
        q = select(Lead).order_by(Lead.updated_at)
        since = parse_dt(updated_since)
        if since:
            q = q.where(Lead.updated_at > since)
        d1, d2 = parse_date(created_from), parse_date(created_to)
        if d1:
            q = q.where(Lead.created_at >= datetime.combine(d1, datetime.min.time()))
        if d2:
            q = q.where(Lead.created_at < datetime.combine(d2 + timedelta(days=1), datetime.min.time()))
        out = []
        for l in s.scalars(q.limit(5000)):
            r = lead_out(l, at)
            row = {k: r[k] for k in ("id", "created_at", "updated_at", "channel", "phone_norm", "name", "source", "utm",
                                     "manager", "status", "refusal_reason", "package", "booking_date",
                                     "prepaid_at", "prepaid_amount", "holiday_date")}
            # тег «Как узнали» отдельно группой и каналом: консоль считает по нему источник
            row["source_group"] = l.source.group if l.source else ""
            row["source_name"] = l.source.name if l.source else ""
            out.append(row)
        return {"now": at.isoformat(), "leads": out}


# способ связи из формы сайта → значение «Куда писать» панели
INTAKE_CONTACTS = {"whatsapp": "WhatsApp", "telegram": "Telegram", "max": "МАКС", "макс": "МАКС",
                   "звонок": "звонок", "телефон": "звонок", "смс": "СМС", "sms": "СМС"}


def _intake_extras(data: dict) -> dict:
    """Дата праздника, возраст, число детей, пакет и способ связи из заявки сайта.
    Что не разобрали — не теряем: уходит строкой в комментарий через поле comment самого сайта."""
    out = {}
    raw_date = _str(data.get("date") or data.get("holiday_date"))
    if raw_date:
        try:
            if parse_date(raw_date):
                out["holiday_date"] = raw_date
        except HTTPException:
            pass
    for key, field in (("age", "child_age"), ("kids", "kids")):
        if _str(data.get(key)):
            out[field] = _str(data.get(key))[:16]
    pkg = _str(data.get("pkg") or data.get("package"))
    if pkg in PACKAGES:
        out["package"] = pkg
    contact = INTAKE_CONTACTS.get(_str(data.get("contact")).lower())
    if contact:
        out["contact_channel"] = contact
    return out


# utm_source заявки с сайта → канал «Как узнали». Ключ ищется в метке целиком или как её часть
# («kp.ru», «2gis_promo»), регистр не важен. Всё, что похоже на Яндекс, — Яндекс Директ.
UTM_SOURCES = {
    "instagram": "Instagram", "ig": "Instagram",
    "2gis": "2ГИС", "dgis": "2ГИС",
    "ozon": "Реклама на Ozon",
    "vk": "Группа ВКонтакте", "vkontakte": "Группа ВКонтакте",
    "kp": "Комсомольская правда", "komsomolskaya": "Комсомольская правда", "komsomolka": "Комсомольская правда",
    "timeout": "Time Out", "time_out": "Time Out", "time-out": "Time Out",
    "google": "Поиск Google",
    "biglion": "Биглион",
}


def _source_by_utm(p: dict) -> str:
    src = (p.get("utm_source") or "").lower().strip()
    if _is_direct(p):
        return "Яндекс Директ"
    key = re.sub(r"^(https?://)?(www\.)?", "", src).split("/")[0]
    if key in UTM_SOURCES:
        return UTM_SOURCES[key]
    for k, name in UTM_SOURCES.items():  # «kp.ru», «2gis_promo», «timeout-msk»
        if len(k) > 2 and k in key or re.match(rf"^{re.escape(k)}[._\-]", key):
            return name
    return "Сайт"


def _is_direct(p: dict) -> bool:
    src = (p.get("utm_source") or "").lower()
    return src in ("ya", "yd") or "yandex" in src or "direct" in src or src.startswith("ya_") or src.startswith("ya.")
