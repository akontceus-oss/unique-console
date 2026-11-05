"""Применение, откат и оценка правок.

Режимы (DIRECT_MODE):
  dry_run — ничего не отправляется в Директ, правка только записывается в журнал (по умолчанию);
  sandbox — правка уходит в песочницу API Директа;
  live    — правка уходит в боевой кабинет.

Правка запоминает, какие кампании она действительно изменила (metrics.changed_campaigns):
откат трогает только их и никогда не убирает то, что было в кампании до платформы.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings, load_rules
from ..models import Action, AdStat, Lead
from ..pipeline.normalize import now_local, today_local
from .state import changed_campaigns, clean_word

_LOCK = threading.Lock()
PG_LOCK_KEY = 724_001  # один ключ на все записи в Директ: правки идут строго по одной


class ActionError(RuntimeError):
    """Правку нельзя выполнить в текущем состоянии (показывается человеку)."""


@contextmanager
def direct_write_lock(session: Session):
    """Сериализует записи в Директ: внутри процесса — мьютексом, между процессами — блокировкой PostgreSQL.

    Блокировка PostgreSQL держится до конца транзакции, поэтому после правки транзакцию нужно закоммитить.
    """
    with _LOCK:
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": PG_LOCK_KEY})
        yield


def _items(value) -> list[str]:
    if isinstance(value, dict):
        return list(value.get("Items") or [])
    return list(value or [])


def _handler(kind: str):
    """Обработчик правок ставок и других новых типов (partyhub/direct/kinds.py)."""
    from ..direct.kinds import HANDLERS  # локальный импорт: kinds сам зависит от этого модуля

    if kind not in HANDLERS:
        raise ActionError(f"неизвестный тип правки {kind}")
    return HANDLERS[kind]


class ActionApplier:
    def __init__(self, session: Session, mode: str | None = None, client=None):
        self.session = session
        self.mode = mode or get_settings().direct_mode
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from ..collectors.direct import client_from_settings

            self._client = client_from_settings(self.mode)
        return self._client

    # ------------------------------------------------------------ apply ---
    def apply(self, action: Action, by: str = "auto", at: datetime | None = None) -> Action:
        at = at or now_local()
        if action.kind == "alert":
            return action
        changed: list[int] = []
        notes: list[str] = []
        try:
            with direct_write_lock(self.session):
                if self.mode == "dry_run":
                    changed = [int(c) for c in action.campaign_ids]
                    notes.append(f"dry-run: {self._describe(action)} — в Директ не отправлялось")
                else:
                    for cid in action.campaign_ids:
                        if self._apply_one(action, int(cid)):
                            changed.append(int(cid))
                            notes.append(f"{cid}: изменено")
                        else:
                            notes.append(f"{cid}: уже было")
            action.status = "applied"
            action.result = f"{self.mode}: " + "; ".join(notes) if self.mode != "dry_run" else notes[0]
        except Exception as e:
            action.status = "partial" if changed else "failed"
            action.result = (f"{self.mode}: " + "; ".join(notes) + "\n" if notes else "") + f"Ошибка: {type(e).__name__}: {e}"
            action.result = action.result[:2000]
        action.metrics = {**(action.metrics or {}), "changed_campaigns": changed}
        if changed or action.status == "applied":
            action.applied_at = at
            action.apply_mode = self.mode
            action.eval_due = at.date() + timedelta(days=7)
        if by != "auto":
            action.decided_by, action.decided_at = by, at
        self.session.flush()
        return action

    def _apply_one(self, a: Action, cid: int) -> bool:
        """Меняет одну кампанию; False — если там уже было. Перечитывает кампанию и проверяет результат."""
        if a.kind not in ("exclude_site", "add_negative"):
            return _handler(a.kind).apply(self.client, a, cid)
        c = self.client.get_campaign(cid)
        target = clean_word(a.target)
        if a.kind == "exclude_site":
            sites = _items(c.get("ExcludedSites"))
            if target in {s.lower() for s in sites}:
                return False
            limit = load_rules()["limits"]["max_excluded_sites"]
            if len(sites) >= limit:
                raise ActionError(f"в кампании {cid} список исключённых площадок заполнен ({len(sites)})")
            self.client.set_excluded_sites(cid, sites + [a.target])
            check = {s.lower() for s in _items(self.client.get_campaign(cid).get("ExcludedSites"))}
        elif a.kind == "add_negative":
            words = _items(c.get("NegativeKeywords"))
            if target in {clean_word(w) for w in words}:
                return False
            self.client.set_negative_keywords(cid, words + [a.target])
            check = {clean_word(w) for w in _items(self.client.get_campaign(cid).get("NegativeKeywords"))}
        else:
            raise ActionError(f"неизвестный тип правки {a.kind}")
        if target not in check:
            raise ActionError(f"Директ принял запрос, но в кампании {cid} изменения не видно")
        return True

    def approve(self, action: Action, by: str) -> Action:
        if action.status != "proposed":
            raise ActionError(f"Правка уже в статусе «{action.status}»")
        action.status = "approved"
        action.decided_by, action.decided_at = by, now_local()
        return self.apply(action, by=by)

    def reject(self, action: Action, by: str) -> Action:
        if action.status != "proposed":
            raise ActionError(f"Правка уже в статусе «{action.status}»")
        action.status = "rejected"
        action.decided_by, action.decided_at = by, now_local()
        self.session.flush()
        return action

    def rollback(self, action: Action, by: str) -> Action:
        if action.status not in ("applied", "partial"):
            raise ActionError("Откатить можно только применённую правку")
        if action.apply_mode not in ("dry_run", self.mode):
            raise ActionError(
                f"Правка сделана в режиме {action.apply_mode}, а платформа сейчас в режиме {self.mode}. "
                "Откатите её в кабинете Директа вручную."
            )
        undone: list[int] = []
        try:
            with direct_write_lock(self.session):
                if action.apply_mode != "dry_run":
                    target = clean_word(action.target)
                    for cid in changed_campaigns(action):
                        c = self.client.get_campaign(cid)
                        if action.kind == "exclude_site":
                            sites = _items(c.get("ExcludedSites"))
                            rest = [s for s in sites if s.lower() != target]
                            if len(rest) != len(sites):
                                self.client.set_excluded_sites(cid, rest)
                        elif action.kind == "add_negative":
                            words = _items(c.get("NegativeKeywords"))
                            rest = [w for w in words if clean_word(w) != target]
                            if len(rest) != len(words):
                                self.client.set_negative_keywords(cid, rest)
                        else:
                            _handler(action.kind).rollback(self.client, action, cid)
                        undone.append(cid)
        except Exception as e:
            action.result = ((action.result or "") + f"\nОшибка отката ({by}): {type(e).__name__}: {e}"
                             + (f"; откачено в кампаниях {undone}" if undone else ""))[:2000]
            self.session.flush()
            raise ActionError(f"Откат не завершён: {e}") from e
        action.status = "rolled_back"
        action.rolled_back_at = now_local()
        action.result = ((action.result or "") + f"\nОткат: {by}, {action.rolled_back_at:%Y-%m-%d %H:%M}")[:2000]
        self.session.flush()
        return action

    # --------------------------------------------------------- details ---
    def _describe(self, a: Action) -> str:
        if a.kind not in ("exclude_site", "add_negative"):
            return f"{_handler(a.kind).describe(a)} в {len(a.campaign_ids)} кампан."
        what = "исключить площадку" if a.kind == "exclude_site" else "добавить минус-слово"
        return f"{what} «{a.target}» в {len(a.campaign_ids)} кампан."


# ------------------------------------------------------------ оценка ---
def _campaign_window(session: Session, cids: list[int], d1: date, d2: date) -> dict:
    booked = set(load_rules()["leads"]["booked_statuses"])
    cost = sum(
        float(r.cost)
        for r in session.query(AdStat).filter(AdStat.campaign_id.in_(cids), AdStat.date.between(d1, d2))
    )
    leads = (
        session.query(Lead)
        .filter(
            Lead.campaign_id.in_(cids),
            Lead.status != "spam",
            Lead.first_touch_at >= datetime.combine(d1, datetime.min.time()),
            Lead.first_touch_at < datetime.combine(d2 + timedelta(days=1), datetime.min.time()),
        )
        .all()
    )
    n, b = len(leads), sum(1 for l in leads if l.status in booked)
    return {"cost": round(cost, 2), "leads": n, "bookings": b, "cpl": round(cost / n, 2) if n else None, "cpb": round(cost / b, 2) if b else None}


def evaluate_due(session: Session, today: date | None = None) -> list[Action]:
    """Через 7 дней после правки сравнивает 7 дней до и после по кампаниям, где она применена."""
    today = today or today_local()
    out = []
    for a in session.query(Action).filter(Action.status.in_(["applied", "partial"]), Action.eval_due <= today, Action.eval_result.is_(None)):
        cids = changed_campaigns(a) or [int(c) for c in a.campaign_ids]
        day0 = a.applied_at.date()
        before = _campaign_window(session, cids, day0 - timedelta(days=7), day0 - timedelta(days=1))
        after = _campaign_window(session, cids, day0, day0 + timedelta(days=6))
        # «Хуже» — только если цена заявки выросла на 25%+ и броней стало меньше: недельные окна шумные.
        verdict = "neutral"
        if before["cpl"] and after["cpl"]:
            delta = (after["cpl"] - before["cpl"]) / before["cpl"]
            if delta <= -0.1:
                verdict = "better"
            elif delta >= 0.25 and after["bookings"] < before["bookings"]:
                verdict = "worse"
        elif before["leads"] and not after["leads"]:
            verdict = "worse"
        a.eval_result = {"before": before, "after": after, "verdict": verdict}
        out.append(a)
    session.flush()
    return out
