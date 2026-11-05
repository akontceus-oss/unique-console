"""Веб-интерфейс и API платформы.

/                      — дашборд (вход по логину и паролю из .env)
/api/datasets          — все показатели для дашборда за период
/api/actions/{id}/...  — одобрить, отклонить или откатить правку (только из самого дашборда)
/api/webhooks/form     — приём заявок с сайта (токен в адресе)
/api/telegram          — нажатия кнопок и сообщения из чата заявок в Telegram (секрет в заголовке)
/api/direct/...        — вкладка «Директ»: инструменты, предложения в журнал, вопросы к ИИ

Пустые или стандартные секреты не принимаются: соответствующая часть просто не работает,
пока их не заполнят в .env.
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from ..config import get_settings, load_rules
from .. import projects as P
from ..projects import current_slug, get_project, reset_current, set_current
from ..db import create_all, init_engine, session_scope
from ..export import datasets
from ..models import Action, CollectorRun, ReportLog, Touch
from ..pipeline.merge import rebuild_leads
from ..pipeline.metrics import period
from ..pipeline.normalize import normalize_phone, now_local, parse_dt, yesterday
from ..reports.daily import build_report
from ..reports.telegram import Telegram
from ..rules.actions import ActionApplier, ActionError

STATIC = Path(__file__).parent / "static"
security = HTTPBasic()
PLACEHOLDERS = {"", "change-me", "change-me-too", "change-me-secret"}


def secret_ok(value: str | None) -> bool:
    return bool(value) and value not in PLACEHOLDERS and len(value) >= 6


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_engine()
    create_all()
    yield


app = FastAPI(title="Пульт маркетинга", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class ProjectPrefix:
    """/p/<slug>/... — кабинет проекта: всё API ниже работает с настройками и базой этого проекта."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] in ("http", "websocket") else ""
        if not path.startswith("/p/"):
            return await self.inner(scope, receive, send)
        slug, _, rest = path[3:].partition("/")
        if not rest and not path.endswith("/"):  # /p/slug → /p/slug/, чтобы относительные адреса страницы работали
            await send({"type": "http.response.start", "status": 307, "headers": [(b"location", f"/p/{slug}/".encode())]})
            return await send({"type": "http.response.body", "body": b""})
        if await run_in_threadpool(get_project, slug) is None:
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": '{"detail":"Нет такого проекта"}'.encode()})
        token = set_current(slug)
        try:
            await run_in_threadpool(create_all)
            scope = {**scope, "path": "/" + rest, "raw_path": ("/" + rest).encode(), "partyhub_project": slug}
            return await self.inner(scope, receive, send)
        finally:
            reset_current(token)


app.add_middleware(ProjectPrefix)


def auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    s = get_settings()
    if not secret_ok(s.dashboard_password):
        raise HTTPException(503, "Задайте DASHBOARD_PASSWORD в .env (не короче 6 символов)")
    ok_user = secrets.compare_digest(credentials.username.encode(), s.dashboard_user.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), s.dashboard_password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def same_origin(request: Request) -> None:
    """Защита от чужих страниц: браузер сам подставляет логин, поэтому изменения принимаем
    только с заголовком X-Requested-With (чужая страница не может его отправить без нашего согласия)."""
    if request.headers.get("x-requested-with") != "partyhub":
        raise HTTPException(403, "Запрос не из дашборда")
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, "Запрос с другого сайта")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index(user: str = Depends(auth)) -> FileResponse:
    """Вне проекта — список проектов, внутри /p/<slug>/ — дашборд проекта."""
    page = "index.html" if current_slug() else "projects.html"
    return FileResponse(STATIC / page, media_type="text/html; charset=utf-8")


# ------------------------------------------------------------ проекты ---
@app.get("/api/projects")
def api_projects(status: str = "active", user: str = Depends(auth)) -> dict:
    """status: active (по умолчанию) или archived."""
    if status not in ("active", "archived"):
        raise HTTPException(422, "status: active или archived")
    out = []
    for p in P.list_projects(archived=status == "archived"):
        view = P.public_view(p)
        out.append({"slug": p.slug, "name": p.name, "url": f"/p/{p.slug}/",
                    "connected": [b["title"] for b in view["blocks"] if b["connected"]],
                    "archived_at": view["archived_at"], "purge_at": view["purge_at"]})
    return {"projects": out, "archive_days": P.ARCHIVE_DAYS}


@app.post("/api/projects/{slug}/{action}")
def api_project_archive(slug: str, action: str, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    """archive — в архив (удалится через ARCHIVE_DAYS дней), restore — вернуть из архива."""
    if current_slug():
        raise HTTPException(400, "Архивом управляют из списка проектов")
    if action not in ("archive", "restore"):
        raise HTTPException(404, "Нет такого действия")
    try:
        p = (P.archive_project if action == "archive" else P.restore_project)(slug)
    except KeyError:
        raise HTTPException(404, "Нет такого проекта")
    view = P.public_view(p)
    return {"slug": p.slug, "archived_at": view["archived_at"], "purge_at": view["purge_at"]}


@app.post("/api/projects")
async def api_project_create(request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    if current_slug():
        raise HTTPException(400, "Проект создаётся из списка проектов")
    body = await request.json()
    try:
        p = await run_in_threadpool(P.create_project, str((body or {}).get("name") or ""), (body or {}).get("slug") or None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"slug": p.slug, "url": f"/p/{p.slug}/"}


def _need_project() -> str:
    slug = current_slug()
    if not slug:
        raise HTTPException(404, "Откройте проект: /p/<проект>/")
    return slug


@app.get("/api/project")
def api_project(request: Request, user: str = Depends(auth)) -> dict:
    view = P.public_view(get_project(_need_project()))
    st = get_settings()
    for b in view["blocks"]:  # что действует сейчас, если поле пустое: общее значение из .env
        for f in b["fields"]:
            if not f["secret"]:
                f["default"] = str(getattr(st, f["key"], "") or "")
    base = str(request.base_url).rstrip("/")
    hook = f"{base}/p/{view['slug']}/api/webhooks"
    view["webhooks"] = {"form": f"{hook}/form?token={view['webhook_token']}", "tilda": f"{hook}/tilda?token={view['webhook_token']}"}
    return view


@app.post("/api/project/settings")
async def api_project_settings(request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    slug = _need_project()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Ожидается объект с полями")
    if body.get("direct_mode") == "live" and get_settings().direct_mode != "live" and body.get("confirm_live") is not True:
        raise HTTPException(409, "Боевой режим включается только с подтверждением")
    body.pop("confirm_live", None)
    try:
        await run_in_threadpool(P.update_settings, slug, body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@app.get("/api/project/metrika/counters")
def api_metrika_counters(user: str = Depends(auth)) -> dict:
    """Счётчики, доступные токену проекта — чтобы выбрать их галочками, а не искать номера."""
    from ..collectors.metrika import MetrikaClient

    _need_project()
    token = get_settings().metrika_token
    if not token:
        raise HTTPException(409, "Сначала сохраните токен Метрики")
    try:
        client = MetrikaClient.for_token(token)
        counters = client.counters()
    except Exception as e:
        raise HTTPException(502, f"Метрика: {type(e).__name__}: {e}")
    return {"counters": counters}


@app.get("/api/project/metrika/goals")
def api_metrika_goals(counter: str = Query(..., pattern=r"^\d+$"), user: str = Depends(auth)) -> dict:
    from ..collectors.metrika import MetrikaClient

    _need_project()
    token = get_settings().metrika_token
    if not token:
        raise HTTPException(409, "Сначала сохраните токен Метрики")
    try:
        return {"goals": MetrikaClient.for_token(token).goals(counter)}
    except Exception as e:
        raise HTTPException(502, f"Метрика: {type(e).__name__}: {e}")


@app.get("/api/project/topvisor/projects")
def api_topvisor_projects(user: str = Depends(auth)) -> dict:
    from ..collectors.topvisor import TopvisorClient

    _need_project()
    s = get_settings()
    if not (s.topvisor_user_id and s.topvisor_api_key):
        raise HTTPException(409, "Сначала сохраните User ID и ключ Топвизора")
    try:
        projects = TopvisorClient(s.topvisor_user_id, s.topvisor_api_key).projects()
    except Exception as e:
        raise HTTPException(502, f"Топвизор: {type(e).__name__}: {e}")
    return {"projects": [{"id": str(p.get("id")), "name": p.get("name") or "", "site": p.get("site") or "",
                          "regions": [{"index": str(r.get("index")), "name": " · ".join(x for x in (r.get("name"), se.get("name")) if x)}
                                      for se in p.get("searchers") or [] for r in se.get("regions") or []]}
                         for p in projects]}


def _topvisor_client():
    from ..collectors.topvisor import TopvisorClient

    s = get_settings()
    if not (s.topvisor_user_id and s.topvisor_api_key):
        raise HTTPException(409, "Сначала сохраните User ID и ключ Топвизора")
    return TopvisorClient(s.topvisor_user_id, s.topvisor_api_key)


@app.get("/api/project/topvisor/schedule")
def api_topvisor_schedule(user: str = Depends(auth)) -> dict:
    """Расписание автопроверки позиций в Топвизоре по выбранным проектам (проверку запускает сам Топвизор)."""
    from ..collectors.topvisor import schedule_of, split_ids

    _need_project()
    client = _topvisor_client()
    wanted = split_ids(get_settings().topvisor_project_id)
    if not wanted:
        raise HTTPException(409, "Сначала выберите проекты Топвизора и нажмите «Сохранить»")
    try:
        names = {str(p.get("id")): p for p in client.projects()}
        out = [{"id": pid, "name": (names.get(pid) or {}).get("name") or "", "site": (names.get(pid) or {}).get("site") or "",
                "schedule": schedule_of(client, pid)} for pid in wanted]
    except Exception as e:
        raise HTTPException(502, f"Топвизор: {type(e).__name__}: {e}")
    return {"projects": out, "collect_time": get_settings().collect_time}


@app.post("/api/project/topvisor/schedule")
async def api_topvisor_schedule_set(request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    """{project_id, days: ["MONDAY", ...], hour, minute}; пустые days — выключить автопроверку проекта."""
    from ..collectors.topvisor import WEEKDAYS, set_schedule, split_ids

    _need_project()
    body = await request.json()
    pid = str((body or {}).get("project_id") or "")
    if pid not in split_ids(get_settings().topvisor_project_id):
        raise HTTPException(422, "Этот проект Топвизора не выбран в подключении")
    days = (body or {}).get("days") or []
    if not isinstance(days, list) or any(d not in WEEKDAYS for d in days):
        raise HTTPException(422, "Дни недели: MONDAY … SUNDAY")
    try:
        hour, minute = int(body.get("hour", 0)), int(body.get("minute", 0))
    except (TypeError, ValueError):
        raise HTTPException(422, "Время: часы 0–23, минуты 0–59")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(422, "Время: часы 0–23, минуты 0–59")
    client = _topvisor_client()
    try:
        rule = await run_in_threadpool(set_schedule, client, pid, days, hour, minute)
    except Exception as e:
        raise HTTPException(502, f"Топвизор: {type(e).__name__}: {e}")
    return {"project_id": pid, "schedule": rule}


# блок «Подключений» → сборщики (jobs.COLLECTORS) и за сколько дней собирать по кнопке
COLLECT_BY_BLOCK = {"direct": (["direct"], 7), "metrika": (["metrika"], 30), "topvisor": (["topvisor"], 30),
                    "callibri": (["callibri"], 30), "bitrix": (["bitrix24", "bitrix_chat"], 30),
                    "yclients": (["yclients", "yclients_file"], 60)}


@app.get("/api/project/collect")
def api_project_collect_status(user: str = Depends(auth)) -> dict:
    """Последний сбор каждого источника — для строки «Последний сбор» на карточках «Подключений»."""
    _need_project()
    with session_scope() as s:
        runs = s.scalars(select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(300)).all()
        latest: dict[str, dict] = {}
        for r in runs:
            latest.setdefault(r.collector, {"status": r.status, "rows": r.rows, "at": r.started_at.isoformat(timespec="minutes"),
                                            "message": (r.message or "").splitlines()[0][:300] if r.message else ""})
    blocks = {}
    for b, (names, _) in COLLECT_BY_BLOCK.items():
        runs = [dict(latest[c], collector=c) for c in names if c in latest]
        if any(r["status"] != "skipped" for r in runs):  # YCLIENTS файлом: «API не настроено» не прячет загрузку
            runs = [r for r in runs if r["status"] != "skipped"]
        blocks[b] = runs
    return {"blocks": blocks,
            "schedule": {"daily": get_settings().collect_time, "leads_minutes": get_settings().leads_sync_minutes}}


@app.post("/api/project/collect/{block}")
def api_project_collect(block: str, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    """Кнопка «Собрать сейчас»: тот же сбор, что утром, но сразу и за больший период."""
    from ..jobs import collect

    _need_project()
    if block not in COLLECT_BY_BLOCK:
        raise HTTPException(404, "Этот блок ничего не собирает")
    names, days = COLLECT_BY_BLOCK[block]
    today = now_local().date()
    res = collect(date_from=today - timedelta(days=days), date_to=today if block != "direct" else yesterday(), only=names)
    if any(c["status"] == "ok" for c in res) and block in ("callibri", "bitrix", "yclients"):
        rebuilder.run_now()  # новые обращения и брони сразу склеиваются в заявки
    return {"results": res, "days": days}


@app.post("/api/project/yclients/import")
async def api_yclients_import(file: UploadFile = File(...), user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    """Кнопка «Загрузить выгрузку» на карточке YCLIENTS: файл Excel/CSV из раздела «Записи»."""
    from ..collectors.yclients import FILE_LIMIT, import_export

    _need_project()
    data = await file.read(FILE_LIMIT + 1)

    def run() -> dict:
        with session_scope() as s:
            return import_export(s, data, file.filename or "")

    try:
        res = await run_in_threadpool(run)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(422, f"Не удалось прочитать файл: {type(e).__name__}: {e}")
    await run_in_threadpool(rebuilder.run_now)  # брони сразу склеиваются с обращениями
    return res


@app.post("/api/project/check/{check_id}")
def api_project_check(check_id: str, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    from ..check import CHECK_BY_ID

    _need_project()
    fn = CHECK_BY_ID.get(check_id)
    if not fn:
        raise HTTPException(404, "Нет такой проверки")
    try:
        status, _, msg = fn().partition(":")
    except Exception as e:
        status, msg = "error", f"{type(e).__name__}: {e}"
    return {"status": status, "message": msg}


@app.get("/api/datasets")
def api_datasets(days: int = Query(30, ge=1, le=365), end: date | None = None, user: str = Depends(auth)) -> JSONResponse:
    d1, d2 = period(days, end=end or yesterday())
    with session_scope() as s:
        data = datasets(s, d1, d2)
    st = get_settings()
    data["meta"] = [{"currency": st.currency_symbol, "business": st.business_name, "mode": st.direct_mode,
                     "from": d1.isoformat(), "to": d2.isoformat()}]
    return JSONResponse(data)


@app.get("/api/health")
def api_health(user: str = Depends(auth)) -> dict:
    with session_scope() as s:
        runs = s.scalars(select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(200)).all()
        latest: dict[str, dict] = {}
        for r in runs:
            latest.setdefault(r.collector, {"collector": r.collector, "status": r.status, "rows": r.rows,
                                            "at": r.started_at.isoformat(timespec="minutes"),
                                            "message": (r.message or "").splitlines()[0] if r.message else ""})
        rep = s.scalars(select(ReportLog).order_by(ReportLog.sent_at.desc()).limit(1)).first()
        return {
            "collectors": list(latest.values()),
            "last_report": {"at": rep.sent_at.isoformat(timespec="minutes"), "delivered": rep.delivered, "error": rep.error} if rep else None,
            "direct_mode": get_settings().direct_mode,
        }


@app.get("/api/report/preview")
def api_report_preview(day: date | None = None, user: str = Depends(auth)) -> dict:
    with session_scope() as s:
        text, ids = build_report(s, day, with_ai=False)
    return {"text": text, "pending_action_ids": ids}


def _decide(action_id: int, verb: str, by: str) -> dict:
    with session_scope() as s:
        # Блокировка строки: двойной клик или кнопка в Telegram одновременно с дашбордом не применят правку дважды.
        a = s.get(Action, action_id, with_for_update=True)
        if not a:
            raise HTTPException(404, "Правка не найдена")
        applier = ActionApplier(s)
        try:
            if verb == "approve":
                applier.approve(a, by=by)
            elif verb == "reject":
                applier.reject(a, by=by)
            elif verb == "rollback":
                applier.rollback(a, by=by)
            else:
                raise HTTPException(400, "Неизвестное действие")
        except ActionError as e:
            s.commit()  # сохраняем записи о частично сделанном
            raise HTTPException(409, str(e)) from e
        return {"id": a.id, "status": a.status, "result": a.result}


@app.post("/api/actions/{action_id}/{verb}")
def api_action(action_id: int, verb: str, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    return _decide(action_id, verb, by=f"{user} (дашборд)")


# ------------------------------------------------------------ заявки с сайта ---
FORM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()
RATE_LIMIT = (20, 600)  # не больше 20 заявок за 10 минут с одного адреса


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > RATE_LIMIT[1]:
            q.popleft()
        if len(q) >= RATE_LIMIT[0]:
            return True
        q.append(now)
        return False


class _DebouncedRebuild:
    """Пересборка заявок не чаще раза в delay секунд, сколько бы форм ни пришло. У каждого проекта — своя."""

    def __init__(self, delay: float = 20.0):
        self.delay = delay
        self._timers: dict[str | None, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self) -> None:
        slug = current_slug()  # таймер работает в другом потоке: проект передаём явно
        with self._lock:
            timer = self._timers.get(slug)
            if timer and timer.is_alive():
                return
            timer = threading.Timer(self.delay, self._run, args=(slug,))
            timer.daemon = True
            self._timers[slug] = timer
            timer.start()

    def _run(self, slug: str | None = None) -> None:
        token = set_current(slug)
        try:
            with session_scope() as s:
                rebuild_leads(s)
        except Exception as e:  # заявка уже сохранена; пересборку сделает утренний цикл
            print(f"Пересборка заявок не удалась ({slug or '—'}): {type(e).__name__}: {e}", flush=True)
        finally:
            reset_current(token)

    def run_now(self) -> None:
        self._run(current_slug())


rebuilder = _DebouncedRebuild()


def _clip(value, size: int) -> str:
    return str(value or "")[:size]


def _save_form(data: dict, system: str = "site") -> dict:
    phone = normalize_phone(_clip(data.get("phone") or data.get("tel"), 64))
    if not phone:
        raise HTTPException(422, "Нет телефона")
    created = parse_dt(_clip(data.get("created_at"), 40)) or now_local()
    extra = {
        _clip(k, 50): (_clip(v, 500) if isinstance(v, str) else v)
        for k, v in list(data.items())[:30]
        if k not in FORM_FIELDS and k not in ("phone", "tel", "name", "token") and isinstance(v, (str, int, float))
    }
    with session_scope() as s:
        ext = _clip(data.get("id") or uuid.uuid4(), 100)
        if s.scalar(select(Touch.id).where(Touch.source == "form", Touch.external_id == ext)):
            return {"ok": True, "duplicate": True}
        s.add(
            Touch(
                source="form",
                system=system,
                external_id=ext,
                created_at=created,
                phone=phone,
                name=_clip(data.get("name"), 255) or None,
                ym_client_id=_clip(data.get("ym_client_id") or data.get("_ym_uid"), 64) or None,
                yclid=_clip(data.get("yclid"), 64) or None,
                extra=extra or None,
                **{f: _clip(data.get(f), 255 if f in ("utm_source", "utm_medium", "utm_campaign") else 500) for f in FORM_FIELDS},
            )
        )
    rebuilder.schedule()
    return {"ok": True}


def _check_hook_token(token: str) -> None:
    if not secret_ok(get_settings().webhook_token):
        raise HTTPException(503, "Задайте WEBHOOK_TOKEN в .env")
    if not secrets.compare_digest(token, get_settings().webhook_token):
        raise HTTPException(403, "Неверный токен")


@app.post("/api/webhooks/form")
async def webhook_form(request: Request, token: str = Query(...)) -> dict:
    _check_hook_token(token)
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "Слишком много заявок с одного адреса")
    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
    else:
        data = dict(await request.form())
    if not isinstance(data, dict):
        raise HTTPException(422, "Ожидается объект с полями заявки")
    return await run_in_threadpool(_save_form, data)


def tilda_fields(form: dict) -> dict | None:
    """Заявка Tilda → поля формы платформы. Имена полей в Tilda задаёт автор формы, поэтому ищем по смыслу.

    None — проверочный запрос Tilda при подключении вебхука (test=test).
    """
    if form.get("test") == "test" and len(form) <= 2:
        return None
    low = {str(k).strip().lower(): v for k, v in form.items() if isinstance(v, str)}
    phone = next((v for k, v in low.items() if "phone" in k or "tel" in k or "телефон" in k), "")
    name = next((v for k, v in low.items() if k in ("name", "имя", "fio", "фио") or k.startswith("name")), "")
    out = {k: v for k, v in low.items() if k not in ("cookies",)}
    for part in str(low.get("cookies") or "").split(";"):  # «Передавать Cookies»: utm-метки и _ym_uid
        k, _, v = part.strip().partition("=")
        if k in FORM_FIELDS or k == "_ym_uid":
            out.setdefault(k, v)
    out.update({"phone": phone, "name": name, "id": "tilda-" + str(low.get("tranid") or uuid.uuid4())})
    return out


@app.post("/api/webhooks/tilda", response_class=HTMLResponse)
async def webhook_tilda(request: Request, token: str = Query(...)) -> HTMLResponse:
    """Вебхук Tilda: Настройки сайта → Формы → Webhook, адрес из экрана «Подключения». Tilda ждёт ответ «ok»."""
    _check_hook_token(token)
    data = tilda_fields(dict(await request.form()))
    if data is not None:
        try:
            await run_in_threadpool(_save_form, data, "tilda")
        except HTTPException as e:
            if e.status_code != 422:  # заявку без телефона Tilda повторять не нужно
                raise
    return HTMLResponse("ok")


# ------------------------------------------------------------ Telegram ---
from ..chats.telegram_in import handle_lead_message, message_of  # noqa: E402

def _handle_callback(cb: dict) -> None:
    s = get_settings()
    tg = Telegram()
    chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    if not s.telegram_chat_id or chat_id != str(s.telegram_chat_id):
        tg.answer_callback(cb["id"], "Этот чат не может управлять правками")
        return
    data = cb.get("data") or ""
    verb = {"ap": "approve", "rj": "reject"}.get(data.split(":")[0])
    try:
        action_id = int(data.split(":")[1])
    except (IndexError, ValueError):
        action_id = None
    if not verb or not action_id:
        tg.answer_callback(cb["id"], "Непонятная кнопка")
        return
    who = _clip((cb.get("from") or {}).get("first_name") or "Telegram", 60)
    try:
        res = _decide(action_id, verb, by=f"{who} (Telegram)")
        msg = {"applied": "Применено", "rejected": "Отклонено", "partial": "Применено частично", "failed": "Не удалось применить"}.get(
            res["status"], res["status"])
    except HTTPException as e:
        msg = str(e.detail)
    tg.answer_callback(cb["id"], msg)


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> dict:
    s = get_settings()
    if not secret_ok(s.telegram_webhook_secret) or not s.telegram_chat_id:
        raise HTTPException(503, "Telegram не настроен: задайте TELEGRAM_WEBHOOK_SECRET и TELEGRAM_CHAT_ID")
    if not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), s.telegram_webhook_secret):
        raise HTTPException(403, "bad secret")
    update = await request.json()
    if not isinstance(update, dict):
        return {"ok": True}
    cb = update.get("callback_query")
    if cb and cb.get("id"):
        await run_in_threadpool(_handle_callback, cb)
    elif (msg := message_of(update)) is not None:
        if await run_in_threadpool(handle_lead_message, msg):
            rebuilder.schedule()
    return {"ok": True}


# ------------------------------------------------------------ вкладка «Директ» ---
from ..ai.providers import LLMError  # noqa: E402
from ..direct import service as direct_service  # noqa: E402


def _period(days: int, end: date | None):
    return period(days, end=end or yesterday())


@app.get("/api/direct/manifest")
def api_direct_manifest(user: str = Depends(auth)) -> dict:
    return direct_service.manifest()


@app.get("/api/direct/tools/{tool_id}")
def api_direct_tool(tool_id: str, days: int = Query(30, ge=1, le=365), end: date | None = None, provider: str = "",
                    user: str = Depends(auth)) -> JSONResponse:
    d1, d2 = _period(days, end)
    with session_scope() as s:
        try:
            res = direct_service.run_tool(s, tool_id, d1, d2, {"provider": provider})
        except KeyError:
            raise HTTPException(404, "Нет такого инструмента")
        except LLMError as e:
            raise HTTPException(502, str(e))
    return JSONResponse(res)


@app.post("/api/direct/tools/{tool_id}/propose")
def api_direct_propose(tool_id: str, days: int = Query(30, ge=1, le=365), end: date | None = None, provider: str = "",
                       user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    d1, d2 = _period(days, end)
    with session_scope() as s:
        try:
            return direct_service.propose(s, tool_id, d1, d2, {"provider": provider})
        except KeyError:
            raise HTTPException(404, "Нет такого инструмента")
        except ValueError as e:
            raise HTTPException(409, str(e))


@app.post("/api/direct/ask")
async def api_direct_ask(request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("question") or "").strip():
        raise HTTPException(422, "Пустой вопрос")
    days = max(1, min(365, int(body.get("days") or 30)))
    d1, d2 = _period(days, None)

    def run():
        with session_scope() as s:
            return direct_service.ask(s, str(body["question"]), d1, d2, str(body.get("provider") or "") or None)

    try:
        return await run_in_threadpool(run)
    except LLMError as e:
        raise HTTPException(502, str(e))


# ------------------------------------------------------------ ИИ-агент Директа ---
from ..direct import agent as direct_agent  # noqa: E402
from ..models import DirectAgentRun  # noqa: E402


@app.post("/api/direct/agent")
async def api_agent_start(request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    body = await request.json()
    task = str((body or {}).get("task") or "").strip()
    if not task:
        raise HTTPException(422, "Пустое задание")
    try:
        run_id = await run_in_threadpool(direct_agent.start, task, str(body.get("provider") or ""), user)
    except LLMError as e:
        raise HTTPException(502, str(e))
    return {"id": run_id}


@app.post("/api/direct/agent/{run_id}/reply")
async def api_agent_reply(run_id: int, request: Request, user: str = Depends(auth), _: None = Depends(same_origin)) -> dict:
    body = await request.json()
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(422, "Пустое сообщение")
    try:
        await run_in_threadpool(direct_agent.reply, run_id, text)
    except KeyError:
        raise HTTPException(404, "Задание не найдено")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"id": run_id}


@app.get("/api/direct/agent")
def api_agent_list(user: str = Depends(auth)) -> dict:
    with session_scope() as s:
        return {"runs": direct_agent.recent_runs(s)}


@app.get("/api/direct/agent/{run_id}")
def api_agent_get(run_id: int, user: str = Depends(auth)) -> JSONResponse:
    with session_scope() as s:
        run = s.get(DirectAgentRun, run_id)
        if not run:
            raise HTTPException(404, "Задание не найдено")
        return JSONResponse(direct_agent.run_view(s, run))


@app.post("/api/direct/changesets/{cs_id}/{verb}")
def api_changeset(cs_id: int, verb: str, user: str = Depends(auth), _: None = Depends(same_origin)) -> JSONResponse:
    fn = {"apply": direct_agent.apply_changeset, "reject": direct_agent.reject_changeset,
          "rollback": direct_agent.rollback_changeset}.get(verb)
    if not fn:
        raise HTTPException(400, "Неизвестное действие")
    with session_scope() as s:
        try:
            cs = fn(s, cs_id, f"{user} (дашборд)")
        except KeyError:
            raise HTTPException(404, "План не найден")
        except direct_agent.PlanError as e:
            raise HTTPException(409, str(e))
        return JSONResponse(direct_agent.changeset_view(cs))


@app.get("/api/rules")
def api_rules(user: str = Depends(auth)) -> dict:
    return load_rules()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "time": now_local().isoformat(timespec="seconds"), "yesterday": (now_local().date() - timedelta(days=1)).isoformat()}
