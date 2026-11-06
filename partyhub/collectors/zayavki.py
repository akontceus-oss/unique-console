"""Панель заявок менеджеров (zayavki.drozdgid.ru): заявки, теги «Как узнали» и брони.

Панель отдаёт заявки по токену: GET <адрес>/api/export/leads?created_from=…&created_to=…
с заголовком X-Export-Token. Каждая заявка панели становится:
- обращением (Touch, system=zayavki) с UTM-метками и тегом «Как узнали» — по тегу сводка
  определяет источник, если меток и колтрекинга нет;
- записью CRM (entity=panel) со статусом менеджера — по ней заявка считается бронью.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CrmRecord, Touch
from ..pipeline.channels import split_tag
from ..pipeline.normalize import normalize_phone, parse_dt
from .base import Collector

# Статусы панели (как в таблице «Брони») → статусы платформы.
STATUS_MAP = {
    "новая": "new",
    "не дозвонились": "in_progress",
    "обсуждаем": "in_progress",
    "информация отправлена": "in_progress",
    "думает": "in_progress",
    "назначена экскурсия": "in_progress",
    "перестал отвечать": "in_progress",
    "бронь оплачена": "prepaid",
    "отказ": "lost",
    "далеко": "lost",
    "просто покататься": "lost",
    "другое": "lost",
    "нецелевой лид": "spam",
}


def panel_status(item: dict) -> str:
    status = STATUS_MAP.get(str(item.get("status") or "").strip().lower(), "in_progress")
    if status in ("new", "in_progress") and (item.get("booking_date") or item.get("prepaid_at")):
        return "booked"  # дату брони вписали, а статус ещё не сменили
    return status


class ZayavkiClient:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        if not base_url or not token:
            raise ValueError("Не заданы адрес панели заявок и токен выгрузки")
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=60)
        self.token = token

    def leads(self, date_from: date, date_to: date) -> list[dict]:
        r = self.http.get(self.base_url + "/api/export/leads",
                          params={"created_from": date_from.isoformat(), "created_to": date_to.isoformat()},
                          headers={"X-Export-Token": self.token})
        if r.status_code == 403:
            raise RuntimeError("панель заявок не приняла токен выгрузки (ZAYAVKI_EXPORT_TOKEN на сервере панели)")
        r.raise_for_status()
        return list(r.json().get("leads") or [])


def parse_leads(items: list[dict], date_from: date, date_to: date) -> tuple[list[Touch], list[CrmRecord]]:
    touches, records = [], []
    start, end = datetime.combine(date_from, datetime.min.time()), datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    for item in items:
        created = parse_dt(item.get("created_at"))
        if not created or not (start <= created < end):  # старая панель фильтр по дате не знает
            continue
        ext = str(item.get("id"))
        raw_phone = item.get("phone_norm") or item.get("phone") or ""
        phone = normalize_phone(("+" + raw_phone) if raw_phone and str(raw_phone).isdigit() else raw_phone)
        utm = item.get("utm") or {}
        tag, tag_group = (item.get("source_name") or "", item.get("source_group") or "")
        if not tag and not tag_group:
            tag, tag_group = split_tag(item.get("source"))
        extra = {k: v for k, v in {"tag": tag, "tag_group": tag_group, "panel_channel": item.get("channel"),
                                   "status": item.get("status")}.items() if v}
        touches.append(Touch(
            source="panel", system="zayavki", external_id=f"lead:{ext}", created_at=created, phone=phone,
            name=item.get("name") or None,
            utm_source=utm.get("source") or "", utm_medium=utm.get("medium") or "", utm_campaign=utm.get("campaign") or "",
            utm_content=utm.get("content") or "", utm_term=utm.get("term") or "",
            manager=item.get("manager") or None, extra=extra,
        ))
        if phone:  # без телефона запись CRM не к чему привязать: осталась бы отдельной заявкой-дублем
            party = parse_dt(item.get("booking_date") or item.get("holiday_date") or "")
            records.append(CrmRecord(
                entity="panel", external_id=ext, phone=phone, title=item.get("name") or None,
                status_raw=str(item.get("status") or "")[:64], status=panel_status(item),
                amount=float(item.get("prepaid_amount") or 0), created_at=created,
                modified_at=parse_dt(item.get("updated_at")), manager=item.get("manager") or None,
                party_date=party.date() if party else None,
                lost_reason=(item.get("refusal_reason") or None),
                utm_source=utm.get("source") or "", utm_campaign=utm.get("campaign") or "", utm_content=utm.get("content") or "",
            ))
    return touches, records


class ZayavkiCollector(Collector):
    name = "zayavki"

    def __init__(self, client: ZayavkiClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(self._client or (s.zayavki_url and s.zayavki_export_token))

    @property
    def client(self) -> ZayavkiClient:
        if self._client is None:
            s = get_settings()
            self._client = ZayavkiClient(s.zayavki_url, s.zayavki_export_token)
        return self._client

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        touches, records = parse_leads(self.client.leads(date_from, date_to), date_from, date_to)
        start = datetime.combine(date_from, datetime.min.time())
        end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
        session.execute(delete(Touch).where(Touch.system == "zayavki", Touch.created_at >= start, Touch.created_at < end))
        session.execute(delete(CrmRecord).where(CrmRecord.entity == "panel", CrmRecord.created_at >= start, CrmRecord.created_at < end))
        # заявку могли перенести датой за край окна — удаляем ещё и по ключам
        keys = [t.external_id for t in touches]
        for i in range(0, len(keys), 500):
            session.execute(delete(Touch).where(Touch.system == "zayavki", Touch.external_id.in_(keys[i : i + 500])))
        ids = [r.external_id for r in records]
        for i in range(0, len(ids), 500):
            session.execute(delete(CrmRecord).where(CrmRecord.entity == "panel", CrmRecord.external_id.in_(ids[i : i + 500])))
        session.flush()
        session.add_all(touches)
        session.add_all(records)
        return len(touches)
