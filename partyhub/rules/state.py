"""Текущее состояние кампаний глазами платформы: что исключено и какие минус-слова уже есть.

Источник правды — последний снимок настроек из Директа. Поверх него добавляются правки
платформы, сделанные после снимка. Учитываются только правки в текущем режиме (DIRECT_MODE):
правки в режиме dry_run в Директ не уходили, поэтому после перехода в live они не считаются
сделанными и будут предложены снова.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Action, CampaignSnapshot

OPERATORS = "-!+\"[]~"


def clean_word(word: str) -> str:
    """'-!работа' → 'работа': операторы Директа не мешают сравнению."""
    return " ".join(w.strip(OPERATORS) for w in str(word).lower().split()).strip()


def changed_campaigns(a: Action) -> list[int]:
    """Кампании, которые правка действительно изменила (не «уже было»)."""
    m = a.metrics or {}
    if "changed_campaigns" in m:
        return [int(c) for c in m["changed_campaigns"]]
    return [int(c) for c in (a.campaign_ids or [])]


def _last_snapshot(session: Session) -> tuple[date | None, list[CampaignSnapshot]]:
    last = session.scalar(select(CampaignSnapshot.date).order_by(CampaignSnapshot.date.desc()).limit(1))
    if not last:
        return None, []
    return last, list(session.scalars(select(CampaignSnapshot).where(CampaignSnapshot.date == last)))


def _platform_layer(session: Session, kind: str, snap_date: date | None):
    """Правки платформы в текущем режиме, которые снимок ещё не отражает: [(добавить?, cid, слово)]."""
    mode = get_settings().direct_mode
    actions = session.scalars(
        select(Action)
        .where(Action.kind == kind, Action.apply_mode == mode, Action.status.in_(["applied", "partial", "rolled_back"]))
        .order_by(Action.applied_at, Action.id)
    ).all()
    events = []
    for a in actions:
        # В dry_run снимок берётся из настоящего кабинета и правок платформы не содержит.
        if a.applied_at and (mode == "dry_run" or snap_date is None or a.applied_at.date() >= snap_date):
            events.append((a.applied_at, True, a))
        if a.status == "rolled_back" and a.rolled_back_at and (mode == "dry_run" or snap_date is None or a.rolled_back_at.date() >= snap_date):
            events.append((a.rolled_back_at, False, a))
    out = []
    for _, add, a in sorted(events, key=lambda e: (e[0], e[2].id)):
        for cid in changed_campaigns(a):
            out.append((add, cid, clean_word(a.target)))
    return out


def excluded_pairs(session: Session) -> set[tuple[int, str]]:
    """{(campaign_id, площадка)} — что исключено сейчас."""
    snap_date, snaps = _last_snapshot(session)
    pairs = {(int(s.campaign_id), x.lower()) for s in snaps for x in (s.data or {}).get("ExcludedSites", []) or []}
    for add, cid, site in _platform_layer(session, "exclude_site", snap_date):
        (pairs.add if add else pairs.discard)((cid, site))
    return pairs


def current_negatives(session: Session) -> dict[int, set[str]]:
    """{campaign_id: {минус-слова без операторов}}."""
    snap_date, snaps = _last_snapshot(session)
    out: dict[int, set[str]] = {int(s.campaign_id): {clean_word(w) for w in (s.data or {}).get("NegativeKeywords", []) or []} for s in snaps}
    for add, cid, word in _platform_layer(session, "add_negative", snap_date):
        bucket = out.setdefault(cid, set())
        (bucket.add if add else bucket.discard)(word)
    return out


def platform_targets(session: Session) -> set[tuple[int, str]]:
    """{(campaign_id, слово/площадка)} — что меняла платформа в текущем режиме (для журнала снимков)."""
    mode = get_settings().direct_mode
    out = set()
    for a in session.scalars(select(Action).where(Action.apply_mode == mode, Action.status.in_(["applied", "partial", "rolled_back"]))):
        for cid in changed_campaigns(a):
            out.add((cid, clean_word(a.target)))
    return out
