"""Битрикс24: лиды и сделки со статусами, суммами и датой праздника.

Подключение — входящий вебхук (Разработчикам → Другое → Входящий вебхук) с правами crm и user.
REST: https://apidocs.bitrix24.ru/
Лиды из открытых линий (чаты) дополнительно становятся обращениями типа «чат».
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings, load_rules
from ..models import CrmRecord, Touch
from ..pipeline.normalize import normalize_phone, now_local, parse_dt
from .base import Collector

LEAD_FIELDS = ["ID", "TITLE", "NAME", "STATUS_ID", "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY", "ASSIGNED_BY_ID",
               "SOURCE_ID", "PHONE", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]
DEAL_FIELDS = ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY", "ASSIGNED_BY_ID", "CONTACT_ID",
               "LEAD_ID", "SOURCE_ID", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]


class BitrixClient:
    def __init__(self, webhook_url: str, http: httpx.Client | None = None):
        if not webhook_url:
            raise ValueError("Не задан BITRIX_WEBHOOK_URL")
        self.base = webhook_url.rstrip("/") + "/"
        self.http = http or httpx.Client(timeout=60)

    def call(self, method: str, params: dict) -> dict:
        r = self.http.post(self.base + method + ".json", json=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Bitrix24 {method}: {data.get('error')} {data.get('error_description', '')}")
        return data

    def list_all(self, method: str, params: dict, limit_pages: int = 200) -> list[dict]:
        """Постраничный обход: по 50 записей, параметр start."""
        out, start = [], 0
        for _ in range(limit_pages):
            data = self.call(method, {**params, "start": start})
            out.extend(data.get("result") or [])
            if "next" not in data:
                break
            start = data["next"]
        return out


def _first_phone(value) -> str | None:
    if isinstance(value, list) and value:
        return normalize_phone(value[0].get("VALUE"))
    if isinstance(value, str):
        return normalize_phone(value)
    return None


def _status(entity: str, raw: str) -> str:
    raw = (raw or "").split(":")[-1]  # у сделок в воронках стадия выглядит как C1:WON
    return load_rules()["crm_status_map"][entity].get(raw, "in_progress")


def _date(value) -> date | None:
    dt = parse_dt(value) if value else None
    return dt.date() if dt else None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class BitrixCollector(Collector):
    name = "bitrix24"

    def __init__(self, client: BitrixClient | None = None):
        self._client = client

    def configured(self) -> bool:
        return bool(self._client or get_settings().bitrix_webhook_url)

    @property
    def client(self) -> BitrixClient:
        if self._client is None:
            self._client = BitrixClient(get_settings().bitrix_webhook_url)
        return self._client

    def _users(self) -> dict[str, str]:
        try:
            users = self.client.list_all("user.get", {"FILTER": {"ACTIVE": True}})
        except Exception:
            return {}
        return {str(u["ID"]): " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x).strip() or f"#{u['ID']}" for u in users}

    def _contacts_phones(self, ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            for c in self.client.list_all("crm.contact.list", {"filter": {"@ID": chunk}, "select": ["ID", "PHONE"]}):
                p = _first_phone(c.get("PHONE"))
                if p:
                    out[str(c["ID"])] = p
        return out

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        """Забирает лиды и сделки, изменённые с date_from: статус старой заявки мог поменяться."""
        s = get_settings()
        since = datetime.combine(date_from, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S")
        users = self._users()
        party_field, guests_field = s.bitrix_party_date_field, s.bitrix_guests_field
        extra = [f for f in (party_field, guests_field) if f]
        chat_markers = [m.upper() for m in load_rules().get("bitrix", {}).get("chat_source_markers", [])]

        leads = self.client.list_all("crm.lead.list", {"filter": {">=DATE_MODIFY": since}, "select": LEAD_FIELDS + extra})
        deals = self.client.list_all("crm.deal.list", {"filter": {">=DATE_MODIFY": since}, "select": DEAL_FIELDS + extra})
        contact_ids = sorted({str(d["CONTACT_ID"]) for d in deals if d.get("CONTACT_ID")})
        phones = self._contacts_phones(contact_ids) if contact_ids else {}
        lead_phone = {str(l["ID"]): _first_phone(l.get("PHONE")) for l in leads}

        n = 0
        for entity, items in (("lead", leads), ("deal", deals)):
            for it in items:
                ext = str(it["ID"])
                rec = session.scalar(select(CrmRecord).where(CrmRecord.entity == entity, CrmRecord.external_id == ext))
                if rec is None:
                    rec = CrmRecord(entity=entity, external_id=ext)
                    session.add(rec)
                raw = it.get("STATUS_ID") if entity == "lead" else it.get("STAGE_ID")
                phone = _first_phone(it.get("PHONE")) if entity == "lead" else (
                    phones.get(str(it.get("CONTACT_ID"))) or lead_phone.get(str(it.get("LEAD_ID")))
                )
                rec.phone = phone or rec.phone
                rec.title = it.get("TITLE")
                rec.status_raw = raw or ""
                rec.status = _status(entity, raw)
                rec.amount = float(it.get("OPPORTUNITY") or 0)
                rec.created_at = parse_dt(it.get("DATE_CREATE")) or rec.created_at or now_local()
                rec.modified_at = parse_dt(it.get("DATE_MODIFY"))
                rec.manager = users.get(str(it.get("ASSIGNED_BY_ID")), rec.manager)
                rec.party_date = _date(it.get(party_field)) if party_field else rec.party_date
                rec.guests = _int(it.get(guests_field)) if guests_field else rec.guests
                rec.utm_source = it.get("UTM_SOURCE") or ""
                rec.utm_campaign = it.get("UTM_CAMPAIGN") or ""
                rec.utm_content = it.get("UTM_CONTENT") or ""
                n += 1

                source_id = str(it.get("SOURCE_ID") or "").upper()
                if entity == "lead" and chat_markers and any(m in source_id for m in chat_markers):
                    session.execute(delete(Touch).where(Touch.source == "chat", Touch.external_id == f"bitrix-lead:{ext}"))
                    session.add(
                        Touch(
                            source="chat",
                            system="bitrix",
                            external_id=f"bitrix-lead:{ext}",
                            created_at=rec.created_at,
                            phone=rec.phone,
                            name=it.get("NAME") or None,
                            utm_source=it.get("UTM_SOURCE") or "",
                            utm_medium=it.get("UTM_MEDIUM") or "",
                            utm_campaign=it.get("UTM_CAMPAIGN") or "",
                            utm_content=it.get("UTM_CONTENT") or "",
                            utm_term=it.get("UTM_TERM") or "",
                            manager=rec.manager,
                        )
                    )
        session.flush()
        return n


def collect_window(date_to: date) -> tuple[date, date]:
    """Статусы броней меняются неделями — по умолчанию смотрим изменения за 45 дней."""
    return date_to - timedelta(days=45), date_to
