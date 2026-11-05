"""Ежедневный отчёт: цифры за вчера, что сделано, что ждёт решения, эффект правок, состояние источников."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.notes import facts_for_ai, generate_notes
from ..config import get_settings, load_rules
from ..models import Action, AdStat, CollectorRun, ReportLog, Touch
from ..pipeline import metrics as M
from ..pipeline.normalize import now_local, yesterday
from .telegram import Telegram, action_buttons, split_message

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _d(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", " ") + " " + get_settings().currency_symbol


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _delta(cur, base) -> str:
    if not base or cur is None:
        return ""
    ch = (cur - base) / base
    sign = "▲ +" if ch > 0 else "▼ −"
    return f" ({sign}{abs(ch) * 100:.0f}% к среднему за 7 дней)"


def _e(s) -> str:
    return html.escape(str(s or ""))


def build_report(session: Session, day: date | None = None, with_ai: bool = True) -> tuple[str, list[int]]:
    day = day or yesterday()
    econ = load_rules()["economics"]
    s = get_settings()
    one = M._totals(session, day, day)
    week = [M._totals(session, day - timedelta(days=i), day - timedelta(days=i)) for i in range(1, 8)]
    avg_cost = sum(w["cost"] for w in week) / 7 if week else None
    d1, d2 = M.period(30, end=day)
    month = M.summary(session, d1, d2)
    t30 = month["totals"]

    lines = [f"<b>{_e(s.business_name)} · {_d(day)}</b>"]
    lines.append(f"Расход: {_money(one['cost'])}{_delta(one['cost'], avg_cost)}")
    lines.append(f"Заявки из Директа: {one['leads_direct']} · брони: {one['bookings_direct']}")
    lines.append(f"Цена заявки: {_money(one['cpl'])} (цель {_money(econ['target_cpl'])})")
    lines.append(
        f"За 30 дней: {t30['leads_direct']} заявок, {t30['bookings_direct']} броней, "
        f"цена брони {_money(t30['cpb'])} (цель {_money(econ['target_cpb'])})"
    )
    blocks = ["\n".join(lines)]

    since = datetime.combine(day + timedelta(days=1), datetime.min.time())
    recent = session.scalars(select(Action).where(Action.created_at >= since - timedelta(hours=1))).all()
    auto = [a for a in recent if a.level == "auto" and a.status == "applied"]
    failed = [a for a in recent if a.status == "failed"]
    alerts = [a for a in recent if a.status == "alert"]
    pending = session.scalars(select(Action).where(Action.status == "proposed").order_by(Action.created_at)).all()

    def short(a: Action) -> str:
        what = {"exclude_site": "Исключена площадка", "add_negative": "Минус-слово"}.get(a.kind, M.KIND_RU.get(a.kind, a.kind))
        return f"— {what} <b>{_e(a.target)}</b>: {_e(a.reason)}"

    if auto:
        blocks.append("<b>Сделано автоматически</b>\n" + "\n".join(short(a) for a in auto[:15]) + (
            f"\n…и ещё {len(auto) - 15}" if len(auto) > 15 else ""))
    if failed:
        blocks.append("<b>Не удалось применить</b>\n" + "\n".join(f"— {_e(a.target)}: {_e(a.result)}" for a in failed[:10]))
    if pending:
        items = []
        for i, a in enumerate(pending[:10], 1):
            what = {"exclude_site": "Исключить площадку", "add_negative": "Добавить минус-слово"}.get(a.kind, M.KIND_RU.get(a.kind, a.kind))
            items.append(f"{i}. {what} <b>{_e(a.target)}</b> — {_e(a.reason)}")
        blocks.append("<b>Ждут вашего решения</b>\n" + "\n".join(items))
    if alerts:
        blocks.append("<b>Сигналы</b>\n" + "\n".join(f"— {_e(a.reason)} ({_e(a.target)})" for a in alerts[:10]))

    evaluated = session.scalars(
        select(Action).where(Action.eval_result.is_not(None), Action.eval_due >= day - timedelta(days=1), Action.eval_due <= day + timedelta(days=1))
    ).all()
    if evaluated:
        verdict_ru = {"better": "лучше", "worse": "хуже — можно откатить в дашборде", "neutral": "без заметных изменений"}
        rows = []
        for a in evaluated[:10]:
            r = a.eval_result or {}
            b, af = r.get("before", {}), r.get("after", {})
            rows.append(f"— {_e(a.target)}: цена заявки {_money(b.get('cpl'))} → {_money(af.get('cpl'))}, {verdict_ru.get(r.get('verdict'), '')}")
        blocks.append("<b>Эффект правок недельной давности</b>\n" + "\n".join(rows))

    day_start = datetime.combine(day, datetime.min.time())
    calls = session.scalars(select(Touch).where(Touch.source == "call", Touch.created_at >= day_start, Touch.created_at < day_start + timedelta(days=1))).all()
    if calls:
        missed = sum(1 for c in calls if c.answered is False)
        blocks.append(f"<b>Обработка</b>\nЗвонков: {len(calls)}, пропущено: {missed}" + (" — проверьте, перезвонили ли" if missed else ""))

    runs = session.scalars(select(CollectorRun).where(CollectorRun.started_at >= day_start).order_by(CollectorRun.started_at.desc())).all()
    latest: dict[str, CollectorRun] = {}
    for r in runs:
        latest.setdefault(r.collector, r)
    bad = [r for r in latest.values() if r.status == "error"]
    if bad:
        blocks.append("<b>Источники с ошибкой</b>\n" + "\n".join(f"— {_e(r.collector)}: {_e((r.message or '').splitlines()[0])}" for r in bad))

    if with_ai:
        notes = generate_notes(facts_for_ai(month, M.campaigns(session, d1, d2), M.placements(session, d1, d2)))
        if notes:
            blocks.append("<b>Наблюдения</b>\n" + _e(notes))

    if s.public_base_url:
        blocks.append(f"Дашборд: {_e(s.public_base_url.rstrip('/'))}/")
    return "\n\n".join(blocks), [a.id for a in pending[:10]]


def send_report(session: Session, day: date | None = None, telegram: Telegram | None = None) -> ReportLog:
    day = day or yesterday()
    text, pending_ids = build_report(session, day)
    tg = telegram or Telegram()
    log = ReportLog(sent_at=now_local(), report_date=day, text=text, delivered=False)
    if not tg.configured:
        log.error = "Telegram не настроен — отчёт сохранён только в базе"
    else:
        try:
            parts = split_message(text)
            for i, part in enumerate(parts):
                tg.send(part, action_buttons(pending_ids) if i == len(parts) - 1 and pending_ids else None)
            log.delivered = True
        except Exception as e:
            log.error = f"{type(e).__name__}: {e}"
    session.add(log)
    session.flush()
    return log


def has_spend(session: Session, day: date) -> bool:
    return session.scalar(select(AdStat.id).where(AdStat.date == day).limit(1)) is not None
