"""Склейка обращений в заявки и привязка статусов из CRM.

Правила:
- все обращения с одного телефона в пределах окна (merge_window_days) — одна заявка;
- источник заявки — первое обращение с UTM-метками (если меток нет — первое обращение);
- статус, сумма, дата праздника — из CRM: сделка важнее лида, продвинутый статус важнее раннего;
- записи CRM без обращений (например, клиент пришёл сам) тоже становятся заявками.
Склейка полностью пересобирается при каждом запуске — результат детерминирован.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..config import load_rules
from ..models import CrmRecord, Lead, Touch
from .channels import classify
from .normalize import parse_campaign_id, parse_utm_content, source_label

STATUS_PRIORITY = {"done": 6, "prepaid": 5, "booked": 4, "in_progress": 3, "new": 2, "lost": 1, "spam": 0}


def _attribution_touch(touches: list[Touch]) -> Touch:
    for t in touches:
        if t.utm_source:
            return t
    return touches[0]


class _CrmTouch:
    """Запись CRM в роли обращения для channels.classify: только её UTM-метка."""

    system = "crm"
    extra = None
    utm_medium = ""

    def __init__(self, rec: CrmRecord):
        self.utm_source = rec.utm_source


def _pick_crm(records: list[CrmRecord]) -> CrmRecord | None:
    if not records:
        return None
    return max(
        records,
        # бронь YCLIENTS — главный источник правды о визите, затем статус менеджера в панели заявок,
        # затем сделка, затем лид
        key=lambda r: (r.entity == "booking", r.entity == "panel", r.entity == "deal", STATUS_PRIORITY.get(r.status, 0), r.modified_at or r.created_at),
    )


def _apply_attribution(lead: Lead, utm_source: str, utm_medium: str, utm_campaign: str, utm_content: str, utm_term: str):
    network, placement = parse_utm_content(utm_content)
    lead.utm_source, lead.utm_medium = utm_source or "", utm_medium or ""
    lead.utm_campaign, lead.utm_content, lead.utm_term = utm_campaign or "", utm_content or "", utm_term or ""
    lead.network = network
    lead.placement = placement
    lead.campaign_id = parse_campaign_id(utm_campaign)
    lead.keyword = utm_term or None
    lead.source_label = source_label(lead.utm_source, lead.utm_medium, network)


def _manager(records: list[CrmRecord]) -> str | None:
    """Кто вёл заявку: менеджер из панели заявок, затем из сделки/лида Битрикс24.
    «Сотрудник» брони YCLIENTS — это комната (Монако, Сингапур…), а не менеджер, поэтому его не берём."""
    order = {"panel": 0, "deal": 1, "lead": 2}
    people = sorted((r for r in records if r.entity != "booking" and r.manager), key=lambda r: order.get(r.entity, 3))
    return people[0].manager if people else None


def _apply_crm(lead: Lead, rec: CrmRecord | None, all_records: list[CrmRecord]):
    if rec is None:
        return
    lead.status = rec.status
    lead.amount = max((float(r.amount or 0) for r in all_records), default=0)
    lead.manager = _manager(all_records) or lead.manager
    lead.party_date = rec.party_date or next((r.party_date for r in all_records if r.party_date), None)
    lead.guests = rec.guests or next((r.guests for r in all_records if r.guests), None)
    lead.crm_external_id = f"{rec.entity}:{rec.external_id}"


def _party_date_from_forms(touches: list[Touch]):
    """Дата праздника из формы сайта — если в CRM её ещё не заполнили."""
    from .normalize import parse_dt

    for t in touches:
        value = (t.extra or {}).get("party_date")
        dt = parse_dt(str(value)) if value else None
        if dt:
            return dt.date()
    return None


def rebuild_leads(session: Session) -> int:
    rules = load_rules()
    window = timedelta(days=rules["leads"]["merge_window_days"])

    session.execute(update(Touch).values(lead_id=None))
    session.execute(delete(Lead))
    session.flush()

    touches = session.scalars(select(Touch).order_by(Touch.created_at)).all()
    crm = session.scalars(select(CrmRecord).order_by(CrmRecord.created_at)).all()

    crm_by_phone: dict[str, list[CrmRecord]] = defaultdict(list)
    for r in crm:
        if r.phone:
            crm_by_phone[r.phone].append(r)
    crm_lead_by_ext = {r.external_id: r for r in crm if r.entity == "lead"}

    def linked(group: list[Touch]) -> list[CrmRecord]:
        """Лиды CRM, из которых сделаны обращения (чаты открытых линий: external_id = bitrix-lead:<id>)."""
        out = []
        for t in group:
            if t.external_id.startswith("bitrix-lead:"):
                rec = crm_lead_by_ext.get(t.external_id.split(":", 1)[1])
                if rec is not None:
                    out.append(rec)
        return out

    # 1. Группы обращений по телефону, разрезанные окном склейки.
    groups: list[list[Touch]] = []
    by_key: dict[str, list[Touch]] = defaultdict(list)
    for t in touches:
        by_key[t.phone or f"nophone:{t.source}:{t.external_id}"].append(t)
    for key_touches in by_key.values():
        current: list[Touch] = []
        for t in key_touches:
            if current and t.created_at - current[-1].created_at > window:
                groups.append(current)
                current = []
            current.append(t)
        if current:
            groups.append(current)

    used_crm: set[int] = set()
    leads_by_phone: dict[str, list[tuple[Lead, list[Touch]]]] = defaultdict(list)
    for group in groups:
        first = group[0]
        attr = _attribution_touch(group)
        lead = Lead(
            phone=first.phone,
            name=next((t.name for t in group if t.name), None),
            first_touch_at=first.created_at,
            last_touch_at=group[-1].created_at,
            channel=first.source,
            touches_count=len(group),
            status="new",
            amount=0,
            manager=next((t.manager for t in group if t.manager), None),
        )
        _apply_attribution(lead, attr.utm_source, attr.utm_medium, attr.utm_campaign, attr.utm_content, attr.utm_term)
        lead.source_channel, lead.source_method = classify(group)
        calls = [t for t in group if t.source == "call"]
        lead.missed_call = bool(calls) and not any(t.answered for t in calls) and all(t.source == "call" for t in group)
        session.add(lead)
        if lead.phone:
            leads_by_phone[lead.phone].append((lead, group))
        else:
            recs = linked(group)
            for r in recs:
                used_crm.add(r.id)
            _apply_crm(lead, _pick_crm(recs), recs)
            session.flush()
            for t in group:
                t.lead_id = lead.id
    session.flush()

    # 2. Статусы из CRM: запись относится к заявке, если создана не раньше чем за 3 дня до первого
    #    обращения, не позже окна склейки после последнего и раньше следующей заявки с этого телефона.
    #    Иначе бронь через полгода «приклеилась» бы к старой заявке и старой площадке.
    for phone, items in leads_by_phone.items():
        items.sort(key=lambda x: x[0].first_touch_at)
        records = [r for r in crm_by_phone.get(phone, []) if r.id not in used_crm]
        for i, (lead, group) in enumerate(items):
            start = lead.first_touch_at - timedelta(days=3)
            end = lead.last_touch_at + window
            if i + 1 < len(items):
                end = min(end, items[i + 1][0].first_touch_at - timedelta(days=1))
            matched = [r for r in records if start <= r.created_at < end and r.id not in used_crm]
            matched += [r for r in linked(group) if r.id not in used_crm and r not in matched]
            for r in matched:
                used_crm.add(r.id)
            _apply_crm(lead, _pick_crm(matched), matched)
            if lead.party_date is None:
                lead.party_date = _party_date_from_forms(group)
            for t in group:
                t.lead_id = lead.id

    # 3. Записи CRM без обращений — тоже заявки (пришли сами, написали в директ и т. п.).
    orphan_by_phone: dict[str, list[CrmRecord]] = defaultdict(list)
    orphans_nophone: list[CrmRecord] = []
    for r in crm:
        if r.id in used_crm:
            continue
        if r.phone:
            orphan_by_phone[r.phone].append(r)
        else:
            orphans_nophone.append(r)
    orphan_groups: list[list[CrmRecord]] = [[r] for r in orphans_nophone]
    for recs in orphan_by_phone.values():  # тоже режем окном склейки: две брони в разные месяцы — две заявки
        cur: list[CrmRecord] = []
        for r in sorted(recs, key=lambda r: r.created_at):
            if cur and r.created_at - cur[-1].created_at > window:
                orphan_groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            orphan_groups.append(cur)
    for records in orphan_groups:
        first = min(records, key=lambda r: r.created_at)
        lead = Lead(
            phone=first.phone,
            name=first.title,
            first_touch_at=first.created_at,
            last_touch_at=max(r.created_at for r in records),
            channel="booking" if first.entity == "booking" else "crm",
            touches_count=0,
            status="new",
            amount=0,
        )
        _apply_attribution(lead, first.utm_source, "", first.utm_campaign, first.utm_content, "")
        lead.source_channel, lead.source_method = classify([_CrmTouch(r) for r in records])
        _apply_crm(lead, _pick_crm(records), records)
        session.add(lead)

    session.flush()
    return session.query(Lead).count()
