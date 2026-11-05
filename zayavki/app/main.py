from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import auth
from .assign import find_repeat, next_day_no, pick_manager, shifts_now
from .clock import now
from .config import get_settings
from .db import get_session, init_engine
from .flags import FLAG_ORDER, FLAGS, is_alert, lead_flag
from .models import (
    CONTACT_CHANNELS,
    DEFAULT_SOURCES,
    PACKAGES,
    REFUSAL_REASONS,
    ROLES,
    STATUSES,
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


def seed_sources(s: Session) -> None:
    if s.scalar(select(func.count(Source.id))):
        return
    for i, (group, name) in enumerate(DEFAULT_SOURCES):
        s.add(Source(group=group, name=name, order=i))


# ---------------------------------------------------------------- доступ


def current_user(request: Request, s: Session = Depends(db)) -> User:
    payload = auth.read_token(request.cookies.get(auth.COOKIE, ""))
    if not payload:
        raise HTTPException(401, "Нужно войти")
    user = s.get(User, payload["u"])
    if not user or not user.active or user.password_hash[-12:] != payload.get("p"):
        raise HTTPException(401, "Нужно войти")
    return user


def staff(user: User = Depends(current_user)) -> User:
    """Администратор или владелец."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Только для администратора")
    return user


def can_see(user: User, lead: Lead) -> bool:
    return user.role in ("owner", "admin") or lead.manager_id == user.id


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
        "name": lead.name,
        "source_id": lead.source_id,
        "source": f"{lead.source.group} · {lead.source.name}" if lead.source else "",
        "source_text": lead.source_text,
        "utm": {k: getattr(lead, "utm_" + k) for k in ("source", "medium", "campaign", "content", "term")},
        "holiday_date": d(lead.holiday_date),
        "holiday_text": lead.holiday_text,
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
        "prepaid_at": d(lead.prepaid_at),
        "prepaid_amount": lead.prepaid_amount,
        "updated_at": d(lead.updated_at),
        "flag": lead_flag(lead, at),
        "alert": is_alert(lead, at),
    }


# поля, которые можно править в таблице, и как их разбирать
EDITABLE = {
    "name": _str, "phone": _str, "source_id": _int, "source_text": _str,
    "holiday_date": parse_date, "holiday_text": _str, "admin_comment": _str,
    "status": _str, "refusal_reason": _str, "contact_channel": _str,
    "child_age": _str, "kids": _str, "adults": _str, "package": _str,
    "next_contact_at": parse_dt, "manager_comment": _str, "review": _str,
    "booking_date": parse_date, "booking_time": _str, "booking_room": _str, "booking_show": _str,
    "prepaid_at": parse_date, "prepaid_amount": _int,
    "manager_id": _int,
}
# правка этих полей = менеджер поработал с заявкой (снимает «Просрочено»)
WORK_FIELDS = set(EDITABLE) - {"manager_id"}


def apply_changes(s: Session, lead: Lead, data: dict, user: User | None, at: datetime) -> list[str]:
    changed = []
    for field, raw in data.items():
        if field not in EDITABLE:
            raise HTTPException(422, f"Поле нельзя менять: {field}")
        value = EDITABLE[field](raw)
        if field == "status" and value not in STATUSES:
            raise HTTPException(422, f"Нет такого статуса: {value}")
        if field == "manager_id":
            if user and user.role == "manager":
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
    fields = {k: v for k, v in data.items() if k in EDITABLE and k not in ("phone", "manager_id", "status")}
    apply_changes(s, lead, fields, None, at)
    lead.touched_at = None
    for k in ("source", "medium", "campaign", "content", "term"):
        setattr(lead, "utm_" + k, _str(data.get("utm_" + k))[:250])
    s.add(LeadEvent(lead_id=lead.id, user_id=user.id if user else None, field="created",
                    new=f"{channel}; менеджер {manager_id or '—'} ({res['reason']})", at=at))
    s.flush()
    return lead, res


def duplicates(s: Session, norm: str, exclude: int | None = None) -> list[dict]:
    if not norm:
        return []
    q = select(Lead).where(Lead.phone_norm == norm).order_by(Lead.created_at.desc()).limit(5)
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
        return FileResponse(STATIC / "index.html")

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
        managers = s.scalars(select(User).where(User.role == "manager").order_by(User.name)).all()
        on_shift = shifts_now(s, at)
        return {
            "now": at.isoformat(),
            "statuses": STATUSES, "packages": PACKAGES, "refusal_reasons": REFUSAL_REASONS,
            "contact_channels": CONTACT_CHANNELS, "flags": FLAGS, "flag_order": FLAG_ORDER,
            "sources": [{"id": x.id, "group": x.group, "name": x.name, "active": x.active} for x in sources],
            "managers": [{"id": m.id, "name": m.name, "color": m.color, "active": m.active} for m in managers],
            "on_shift": [{"manager_id": sh.manager_id, "name": sh.manager.name, "start": sh.start, "end": sh.end} for sh in on_shift],
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
        if user.role == "manager":
            query = query.where(Lead.manager_id == user.id)
        elif manager_id:
            query = query.where(Lead.manager_id == manager_id)
        if status:
            query = query.where(Lead.status == status)
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
        leads = s.scalars(query).all()
        rows = [lead_out(l, at) for l in leads]
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
        return {"phone_norm": norm, "valid": bool(norm), "formatted": format_phone(norm),
                "duplicates": duplicates(s, norm),
                "suggested": {"manager_id": a.manager_id, "manager": mgr.name if mgr else "", "late": a.late, "reason": a.reason}}

    @app.post("/api/leads")
    def new_lead(body: dict, user: User = Depends(current_user), s: Session = Depends(db)):
        at = now()
        lead, res = create_lead(s, body, user, "manual", at)
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
        at = now()
        changed = apply_changes(s, lead, body, user, at)
        if "manager_id" in changed:
            lead.taken_at = None
            lead.late = False
        s.flush()
        return {"lead": lead_out(lead, at), "changed": changed}

    @app.post("/api/leads/{lead_id}/take")
    def take_lead(lead_id: int, user: User = Depends(current_user), s: Session = Depends(db)):
        lead = s.get(Lead, lead_id)
        if not lead or not can_see(user, lead):
            raise HTTPException(404, "Заявка не найдена")
        at = now()
        if lead.taken_at is None:
            lead.taken_at = at
            lead.touched_at = at
            if lead.manager_id is None and user.role == "manager":
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
        due_q = select(Lead).where(Lead.next_contact_at > since_dt, Lead.next_contact_at <= at)
        if user.role == "manager":
            new_q, due_q = new_q.where(mine), due_q.where(mine)
        else:
            due_q = due_q.where(mine)
        return {"now": at.isoformat(),
                "new": [lead_out(l, at) for l in s.scalars(new_q)],
                "due": [lead_out(l, at) for l in s.scalars(due_q)]}

    # --- менеджеры и пользователи

    @app.get("/api/users")
    def list_users(user: User = Depends(staff), s: Session = Depends(db)):
        return [user_out(u) for u in s.scalars(select(User).order_by(User.role, User.name))]

    @app.post("/api/users")
    def add_user(body: dict, user: User = Depends(staff), s: Session = Depends(db)):
        login_ = _str(body.get("login")).lower()
        role = _str(body.get("role")) or "manager"
        if role not in ROLES:
            raise HTTPException(422, "Роль: owner, admin или manager")
        if role == "owner" and user.role != "owner":
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
    def edit_user(user_id: int, body: dict, user: User = Depends(staff), s: Session = Depends(db)):
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Не найден")
        if u.role == "owner" and user.role != "owner":
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
            if role not in ROLES or (role == "owner" and user.role != "owner"):
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
    def put_shift(body: dict, user: User = Depends(staff), s: Session = Depends(db)):
        day = parse_date(body.get("day"))
        mid = _int(body.get("manager_id"))
        m = s.get(User, mid) if mid else None
        if not day or not m or m.role != "manager":
            raise HTTPException(422, "Нужны день и менеджер")
        start, end = parse_hm(body.get("start") or "08:00"), parse_hm(body.get("end") or "22:00")
        if start >= end:
            raise HTTPException(422, "Конец смены позже начала")
        sh = s.scalar(select(Shift).where(Shift.day == day, Shift.manager_id == mid))
        if not sh:
            sh = Shift(day=day, manager_id=mid)
            s.add(sh)
        sh.start, sh.end = start, end
        s.flush()
        return {"id": sh.id}

    @app.delete("/api/shifts/{shift_id}")
    def delete_shift(shift_id: int, user: User = Depends(staff), s: Session = Depends(db)):
        sh = s.get(Shift, shift_id)
        if sh:
            s.delete(sh)
        return {"ok": True}

    @app.post("/api/shifts/copy-week")
    def copy_week(body: dict, user: User = Depends(staff), s: Session = Depends(db)):
        """Скопировать смены недели (с понедельника src) на неделю с понедельника dst. Занятые дни не трогает."""
        src, dst = parse_date(body.get("src")), parse_date(body.get("dst"))
        if not src or not dst:
            raise HTTPException(422, "Нужны две даты")
        shift_days = (dst - src).days
        made = 0
        for sh in s.scalars(select(Shift).where(Shift.day >= src, Shift.day < src + timedelta(days=7))).all():
            day = sh.day + timedelta(days=shift_days)
            if not s.scalar(select(Shift).where(Shift.day == day, Shift.manager_id == sh.manager_id)):
                s.add(Shift(day=day, manager_id=sh.manager_id, start=sh.start, end=sh.end))
                made += 1
        return {"created": made}

    # --- источники «Как узнали»

    @app.post("/api/sources")
    def add_source(body: dict, user: User = Depends(staff), s: Session = Depends(db)):
        group, name = _str(body.get("group")), _str(body.get("name"))
        if not group or not name:
            raise HTTPException(422, "Нужны группа и название")
        order = (s.scalar(select(func.max(Source.order))) or 0) + 1
        src = Source(group=group, name=name, order=order)
        s.add(src)
        s.flush()
        return {"id": src.id}

    @app.patch("/api/sources/{source_id}")
    def edit_source(source_id: int, body: dict, user: User = Depends(staff), s: Session = Depends(db)):
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

    @app.get("/api/stats")
    def stats(date_from: str | None = None, date_to: str | None = None, user: User = Depends(staff), s: Session = Depends(db)):
        at = now()
        df = parse_date(date_from) or (at.date() - timedelta(days=30))
        dt = parse_date(date_to) or at.date()
        leads = s.scalars(select(Lead).where(Lead.created_at >= datetime.combine(df, datetime.min.time()),
                                             Lead.created_at < datetime.combine(dt + timedelta(days=1), datetime.min.time()))).all()

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
            "source_text": _str(data.get("formname") or data.get("form") or site),
            **{"utm_" + k: _str(data.get("utm_" + k)) for k in ("source", "medium", "campaign", "content", "term")},
        }
        src_name = _str(data.get("source_name"))
        if src_name:
            src = s.scalar(select(Source).where(Source.name == src_name))
            if src:
                payload["source_id"] = src.id
        if "source_id" not in payload:
            src = s.scalar(select(Source).where(Source.name == ("Яндекс Директ" if _is_direct(payload) else "Сайт")))
            if src:
                payload["source_id"] = src.id
        at = now()
        lead, res = create_lead(s, payload, None, site[:32], at)
        return {"ok": True, "id": lead.id, "assignment": res}

    # --- выгрузка для консоли

    @app.get("/api/export/leads")
    def export_leads(request: Request, updated_since: str | None = None, s: Session = Depends(db)):
        token = request.query_params.get("token") or request.headers.get("x-export-token", "")
        cfg = get_settings()
        if not cfg.export_token or token != cfg.export_token:
            raise HTTPException(403, "Неверный токен")
        at = now()
        q = select(Lead).order_by(Lead.updated_at)
        since = parse_dt(updated_since)
        if since:
            q = q.where(Lead.updated_at > since)
        out = []
        for l in s.scalars(q.limit(5000)):
            r = lead_out(l, at)
            out.append({k: r[k] for k in ("id", "created_at", "updated_at", "channel", "phone_norm", "name", "source", "utm",
                                          "manager", "status", "refusal_reason", "package", "booking_date",
                                          "prepaid_at", "prepaid_amount", "holiday_date")})
        return {"now": at.isoformat(), "leads": out}


def _is_direct(p: dict) -> bool:
    src = (p.get("utm_source") or "").lower()
    return src in ("yandex", "direct", "yandex_direct", "ya") or "yandex" in src
