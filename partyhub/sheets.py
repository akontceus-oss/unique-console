"""Google-таблица заявок: одна строка на заявку после склейки.

Столбцы: дата заявки, телефон, имя клиента, источник заявки, UTM-метка, комментарий и служебный ID
(его можно скрыть, но не удалять: по нему строка обновляется, а не дублируется).

Что платформа меняет в таблице:
- новая заявка — новая строка внизу;
- у известной заявки дописываются только пустые ячейки имени, источника и UTM;
- дату, телефон и комментарий после создания строки платформа не трогает: их правят люди.
Если ID строки потерялся, заявка узнаётся по телефону в окне склейки (rules.yaml → leads.merge_window_days).

Доступ — сервисный аккаунт Google: JSON-ключ (GOOGLE_SERVICE_ACCOUNT_FILE), таблица расшарена на его почту
с правом редактирования, ID таблицы — GOOGLE_SHEET_ID.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings, load_rules
from .models import CrmRecord, Lead, Touch
from .pipeline.normalize import now_local

API = "https://sheets.googleapis.com/v4/spreadsheets/"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
HEADER = ["Дата заявки", "Телефон", "Имя клиента", "Источник заявки", "UTM-метка", "Комментарий", "ID"]
FILLABLE = (2, 3, 4)  # имя, источник, UTM: дописываются, если ячейка пуста
CHANNELS = {
    ("call", ""): "Звонок",
    ("form", "site"): "Форма на сайте",
    ("form", "callibri"): "Форма (Callibri)",
    ("chat", "callibri"): "Чат на сайте",
    ("chat", "bitrix"): "Открытая линия Битрикс24",
    ("chat", "bitrix_chat"): "Чат Битрикс24",
    ("chat", "telegram"): "Telegram",
    ("crm", ""): "Битрикс24 CRM",
}
COMMENT_KEYS = ("comment", "message", "text", "question", "comments", "form_name")


def sheet_rules() -> dict:
    return {"days_back": 30, "autofill_comment": True, "skip_statuses": ["spam"], **(load_rules().get("sheets") or {})}


def configured() -> bool:
    s = get_settings()
    return bool(s.google_sheet_id and s.google_service_account_file)


# ------------------------------------------------------------ клиент Sheets API ---
class SheetsClient:
    def __init__(self, sheet_id: str, token_provider=None, http: httpx.Client | None = None):
        self.sheet_id = sheet_id
        self.http = http or httpx.Client(timeout=60)
        self._token_provider = token_provider or service_account_token
        self._token: tuple[str, float] | None = None

    def _headers(self) -> dict:
        if not self._token or self._token[1] < time.time() + 60:
            self._token = self._token_provider(self.http)
        return {"Authorization": f"Bearer {self._token[0]}"}

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, API + self.sheet_id + path, headers=self._headers(), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise RuntimeError(f"Google Sheets {r.status_code}: {msg[:300]}")
        return r.json() if r.content else {}

    def tabs(self) -> list[str]:
        data = self._req("GET", "", params={"fields": "sheets.properties.title"})
        return [sh["properties"]["title"] for sh in data.get("sheets", [])]

    def add_tab(self, title: str) -> None:
        self._req("POST", ":batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": title}}}]})

    def get(self, rng: str) -> list[list]:
        data = self._req("GET", "/values/" + quote(rng, safe=""),
                         params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"})
        return data.get("values", [])

    def put(self, rng: str, rows: list[list]) -> None:
        self._req("PUT", "/values/" + quote(rng, safe=""), params={"valueInputOption": "USER_ENTERED"},
                  json={"values": rows})

    def append(self, rng: str, rows: list[list]) -> None:
        self._req("POST", "/values/" + quote(rng, safe="") + ":append",
                  params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, json={"values": rows})

    def batch_put(self, data: list[dict]) -> None:
        if data:
            self._req("POST", "/values:batchUpdate", json={"valueInputOption": "USER_ENTERED", "data": data})


def service_account_token(http: httpx.Client) -> tuple[str, float]:
    """Токен по JSON-ключу сервисного аккаунта (JWT, подписанный ключом аккаунта)."""
    from google.auth import crypt, jwt

    with open(get_settings().google_service_account_file, encoding="utf-8") as f:
        info = json.load(f)
    now = int(time.time())
    assertion = jwt.encode(crypt.RSASigner.from_service_account_info(info), {
        "iss": info["client_email"], "scope": SCOPE, "aud": info["token_uri"], "iat": now, "exp": now + 3600})
    r = http.post(info["token_uri"], data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
    if r.status_code >= 400:
        raise RuntimeError(f"Google: не удалось получить токен сервисного аккаунта: {r.text[:300]}")
    data = r.json()
    return data["access_token"], time.time() + int(data.get("expires_in", 3600))


def client_from_settings() -> SheetsClient:
    return SheetsClient(get_settings().google_sheet_id)


# ------------------------------------------------------------ строки заявок ---
def _text(value) -> str:
    """Защита от формул: текст из чатов, начинающийся с = + - @, Sheets иначе выполнил бы как формулу."""
    value = str(value or "").strip()
    return "'" + value if value[:1] in ("=", "+", "-", "@", "'") else value


def _phone_display(phone: str | None) -> str:
    if not phone:
        return ""
    try:
        return phonenumbers.format_number(phonenumbers.parse(phone, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return phone


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def lead_key(lead: Lead, first: Touch | None) -> str:
    if first is not None:
        return f"{first.system or first.source}:{first.external_id}"[:120]
    return f"crm:{lead.crm_external_id or lead.id}"


def source_text(lead: Lead, first: Touch | None) -> str:
    if first is not None:
        channel = CHANNELS.get((first.source, first.system)) or CHANNELS.get((first.source, ""), first.source)
    else:
        channel = CHANNELS[("crm", "")]
    label = lead.source_label or ""
    return channel if not label or label == "Прямые / неизвестно" else f"{channel} · {label}"


def utm_text(lead: Lead) -> str:
    return " / ".join(v for v in (lead.utm_source, lead.utm_medium, lead.utm_campaign, lead.utm_content, lead.utm_term) if v)


def comment_text(lead: Lead, first: Touch | None, crm_title: str | None) -> str:
    if first is None:
        return crm_title or ""
    if lead.missed_call:
        return "Пропущенный звонок"
    extra = first.extra or {}
    for k in COMMENT_KEYS:
        if extra.get(k):
            return str(extra[k])[:1000]
    return ""


def lead_rows(session: Session, since: datetime) -> list[dict]:
    rules = sheet_rules()
    leads = session.scalars(select(Lead).where(Lead.first_touch_at >= since).order_by(Lead.first_touch_at)).all()
    firsts: dict[int, Touch] = {}
    for t in session.scalars(select(Touch).where(Touch.lead_id.in_([l.id for l in leads])).order_by(Touch.created_at)):
        firsts.setdefault(t.lead_id, t)
    titles = {f"{r.entity}:{r.external_id}": r.title for r in session.scalars(select(CrmRecord))} if leads else {}
    out = []
    for lead in leads:
        if lead.status in rules["skip_statuses"]:
            continue
        first = firsts.get(lead.id)
        out.append({
            "key": lead_key(lead, first),
            "at": lead.first_touch_at,
            "phone": lead.phone,
            "values": [
                lead.first_touch_at.strftime("%Y-%m-%d %H:%M"),
                "'" + _phone_display(lead.phone) if lead.phone else "",
                _text(lead.name),
                _text(source_text(lead, first)),
                _text(utm_text(lead)),
                _text(comment_text(lead, first, titles.get(lead.crm_external_id or "")) if rules["autofill_comment"] else ""),
            ],
        })
    return out


def _serial_to_dt(value) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(value))
    return None


def sync(session: Session, client: SheetsClient | None = None, since: datetime | None = None) -> dict:
    s = get_settings()
    client = client or client_from_settings()
    tab = s.google_sheet_tab
    rng = lambda a1: f"'{tab}'!{a1}"  # noqa: E731
    if tab not in client.tabs():
        client.add_tab(tab)
    existing = client.get(rng("A1:G"))
    if not existing or [str(c) for c in existing[0][:7]] != HEADER:
        if not existing or not any(existing[0]):
            client.put(rng("A1:G1"), [HEADER])
            existing = [HEADER]
        else:
            raise RuntimeError(f"В листе «{tab}» первая строка не совпадает с шапкой платформы — проверьте лист")

    by_key: dict[str, int] = {}
    by_phone: dict[str, list[tuple[int, datetime | None]]] = {}
    for i, row in enumerate(existing[1:], start=2):
        row = list(row) + [""] * (7 - len(row))
        if row[6]:
            by_key[str(row[6])] = i
        if _digits(row[1]):
            by_phone.setdefault(_digits(row[1]), []).append((i, _serial_to_dt(row[0])))

    window = timedelta(days=load_rules()["leads"]["merge_window_days"])
    since = since or now_local() - timedelta(days=sheet_rules()["days_back"])
    new_rows, updates = [], []
    for item in lead_rows(session, since):
        rownum = by_key.get(item["key"])
        if rownum is None and item["phone"]:
            for i, at in by_phone.get(_digits(item["phone"]), []):
                if at is None or abs(at - item["at"]) <= window:
                    rownum = i
                    break
        if rownum is None:
            new_rows.append(item["values"] + [item["key"]])
            continue
        current = list(existing[rownum - 1]) + [""] * 7
        for col in FILLABLE:
            if item["values"][col] and not str(current[col]).strip():
                updates.append({"range": rng(f"{'ABCDEFG'[col]}{rownum}"), "values": [[item["values"][col]]]})
        if not str(current[6]).strip():
            updates.append({"range": rng(f"G{rownum}"), "values": [[item["key"]]]})
    client.batch_put(updates)
    if new_rows:
        client.append(rng("A1:G1"), new_rows)
    return {"added": len(new_rows), "updated_cells": len(updates)}
