"""Задания по расписанию: утренний сбор и правила, отчёт, офлайн-конверсии, частый сбор заявок."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .collectors.base import run_collector
from .collectors.bitrix import BitrixCollector
from .collectors.bitrix_chat import BitrixChatCollector
from .collectors.callibri import CallibriCollector
from .collectors.direct import DirectCollector
from .collectors.metrika import MetrikaCollector, upload_conversions
from .collectors.topvisor import TopvisorCollector
from .collectors.yclients import YclientsCollector
from .collectors.zayavki import ZayavkiCollector
from .config import get_settings
from .db import session_scope
from .pipeline.merge import rebuild_leads
from .pipeline.normalize import now_local, today_local, yesterday
from .reports.daily import send_report
from .rules.actions import ActionApplier, evaluate_due
from .rules.engine import run_rules

COLLECTORS = {
    "direct": DirectCollector,
    "metrika": MetrikaCollector,
    "callibri": CallibriCollector,
    "bitrix24": BitrixCollector,
    "bitrix_chat": BitrixChatCollector,
    "topvisor": TopvisorCollector,
    "yclients": YclientsCollector,
    "zayavki": ZayavkiCollector,
}

# Сколько дней назад пересобирать: поздние правки источников (статусы, конверсии) подтягиваются.
LOOKBACK = {"direct": 3, "metrika": 3, "callibri": 3, "bitrix24": 45, "bitrix_chat": 3, "topvisor": 3, "yclients": 60,
            "zayavki": 60}


def collect(session: Session | None = None, date_from: date | None = None, date_to: date | None = None,
            only: list[str] | None = None) -> list[dict]:
    """Каждый источник — в своей транзакции: сбой одного не откатывает остальные."""
    date_to = date_to or yesterday()
    out = []
    for name, cls in COLLECTORS.items():
        if only and name not in only:
            continue
        d1 = date_from or (date_to - timedelta(days=LOOKBACK[name] - 1))
        if session is not None:
            run = run_collector(session, cls(), d1, date_to)
            session.commit()
        else:
            with session_scope() as s:
                run = run_collector(s, cls(), d1, date_to)
        out.append({"collector": name, "status": run.status, "rows": run.rows,
                    "message": (run.message or "").splitlines()[0] if run.message else ""})
    return out


LEAD_SOURCES = ("callibri", "bitrix24", "yclients", "zayavki")
# Частый сбор: только источники заявок, за вчера и сегодня (вчера — чтобы не потерять заявки около полуночи).
FAST_SOURCES = ("callibri", "bitrix24", "bitrix_chat", "yclients", "zayavki")


def sync_sheet() -> dict | None:
    from . import sheets

    if not sheets.configured():
        return None
    try:
        with session_scope() as s:
            return sheets.sync(s)
    except Exception as e:  # таблица — витрина: её сбой не должен ронять сбор
        return {"error": f"{type(e).__name__}: {e}"}


def leads_sync() -> dict:
    """Каждые LEADS_SYNC_MINUTES минут: свежие обращения → склейка → Google-таблица."""
    today = today_local()
    result = {"collect": collect(date_from=today - timedelta(days=1), date_to=today, only=list(FAST_SOURCES))}
    with session_scope() as s:
        result["leads"] = rebuild_leads(s)
    result["sheet"] = sync_sheet()
    return result


def daily(day: date | None = None) -> dict:
    """Утренний цикл: сбор → склейка заявок → оценка правок → правила → инструменты Директа → офлайн-конверсии.

    Каждый этап фиксируется отдельно. Если источник заявок (Callibri, Битрикс24) не обновился,
    автоправки не выполняются: без свежих заявок хорошая площадка выглядит как «без заявок».
    """
    day = day or yesterday()
    result = {"collect": collect(date_to=day)}
    failed_leads = [c["collector"] for c in result["collect"] if c["collector"] in LEAD_SOURCES and c["status"] == "error"]
    with session_scope() as s:
        result["leads"] = rebuild_leads(s)
    with session_scope() as s:
        result["evaluated"] = len(evaluate_due(s, today=day + timedelta(days=1)))
    with session_scope() as s:
        hold = f"Не обновились заявки из: {', '.join(failed_leads)}" if failed_leads else ""
        result["rules"] = run_rules(s, ActionApplier(s), day=day, allow_auto=not failed_leads, hold_reason=hold)
    with session_scope() as s:
        from .direct.service import daily_tools

        result["direct_tools"] = daily_tools(s, day)
    st = get_settings()
    if st.metrika_token and st.metrika_counter_id:
        try:
            with session_scope() as s:
                result["conversions"] = upload_conversions(s)
        except Exception as e:
            result["conversions"] = {"error": f"{type(e).__name__}: {e}"}
    result["sheet"] = sync_sheet()
    return result


def report(day: date | None = None) -> dict:
    with session_scope() as s:
        log = send_report(s, day)
        return {"delivered": log.delivered, "error": log.error, "chars": len(log.text)}


# ------------------------------------------------------------ планировщик ---
def _next_run(hhmm: str, now: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    run = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def _ran_today(name: str) -> bool:
    """Было ли задание сегодня: сбор — по журналу сборщиков, отчёт — по журналу отчётов."""
    from sqlalchemy import select

    from .models import CollectorRun, ReportLog

    start = datetime.combine(today_local(), datetime.min.time())
    with session_scope() as s:
        if name == "daily":  # Директ собирается только утренним циклом (при пустом токене — запись «skipped»)
            return s.scalar(select(CollectorRun.id).where(CollectorRun.started_at >= start,
                                                         CollectorRun.collector == "direct").limit(1)) is not None
        return s.scalar(select(ReportLog.id).where(ReportLog.sent_at >= start).limit(1)) is not None


def _stamp() -> str:
    """Время для журнала. Не через now_local(): с неверным часовым поясом проекта она сама падает."""
    try:
        return f"{now_local():%Y-%m-%d %H:%M}"
    except Exception:
        return f"{datetime.now():%Y-%m-%d %H:%M} (UTC?)"


def _run(name: str) -> None:
    from .projects import current_slug

    who = current_slug() or "—"
    try:
        res = {"daily": daily, "report": report, "leads": leads_sync}[name]()
        print(f"{_stamp()} [{who}] {name}: {res}", flush=True)
    except Exception as e:  # планировщик не должен падать из-за одной ошибки
        print(f"{_stamp()} [{who}] {name} упал: {type(e).__name__}: {e}", flush=True)


def _project_slugs() -> list[str | None]:
    """Активные проекты (архивные не собираются и не шлют отчёты); если проектов нет совсем —
    общая база из .env (как до появления проектов)."""
    from .projects import list_projects

    active = [p.slug for p in list_projects()]
    return active if active or list_projects(archived=None) else [None]


def _purge_archive() -> None:
    from .projects import ARCHIVE_DAYS, purge_expired

    try:
        for slug in purge_expired():
            print(f"{_stamp()} проект {slug} удалён: {ARCHIVE_DAYS} дней в архиве", flush=True)
    except Exception as e:
        print(f"{_stamp()} удаление архивных проектов упало: {type(e).__name__}: {e}", flush=True)


def _tick(slug: str | None, last_leads: dict) -> None:  # last_leads: и время сбора заявок, и попытки за день
    """Один проход планировщика по проекту: что пора и ещё не сделано сегодня — делаем."""
    s = get_settings()
    now = now_local()
    for name, hhmm in (("daily", s.collect_time), ("report", s.report_time)):
        h, m = (int(x) for x in hhmm.split(":"))
        key = (slug, name)
        if last_leads.get(key) == now.date():  # уже пробовали сегодня: при ошибке не повторяем каждые 30 секунд
            continue
        if now >= now.replace(hour=h, minute=m, second=0, microsecond=0) and not _ran_today(name):
            last_leads[key] = now.date()
            _run(name)
    every = timedelta(minutes=s.leads_sync_minutes) if s.leads_sync_minutes > 0 else None
    if every and (slug not in last_leads or now - last_leads[slug] >= every):
        last_leads[slug] = now
        _run("leads")


def scheduler_loop(poll_seconds: int = 30) -> None:
    """Планировщик для docker-compose: по каждому проекту сбор в COLLECT_TIME и отчёт в REPORT_TIME
    (время проекта), свежие заявки и Google-таблица — каждые LEADS_SYNC_MINUTES минут.

    Пропущенное за сегодня (сервер был выключен) выполняется при первом проходе. Новые проекты
    из кабинета подхватываются без перезапуска.
    """
    from .projects import reset_current, set_current

    print(f"Планировщик запущен, проверка каждые {poll_seconds} с", flush=True)
    last_leads: dict = {}
    last_purge = 0.0
    while True:
        if time.monotonic() - last_purge >= 3600:  # архив проверяем раз в час
            last_purge = time.monotonic()
            _purge_archive()
        for slug in _project_slugs():
            token = set_current(slug)
            try:
                from .db import create_all

                create_all()
                _tick(slug, last_leads)
            except Exception as e:  # один проект не должен останавливать остальные
                print(f"{_stamp()} проект {slug}: {type(e).__name__}: {e}", flush=True)
            finally:
                reset_current(token)
        time.sleep(poll_seconds)


def is_today(d: date) -> bool:
    return d == today_local()
