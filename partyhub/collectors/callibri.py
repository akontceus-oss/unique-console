"""Callibri: звонки, заявки с форм и чаты.

Документация: https://callibri.ru/help/api_webhook/kak_vospolzovatsya_api
- доступ: права «Аналитика» и «Настройка», токен приходит на email после запроса в настройках;
- метод site_get_statistics отдаёт не больше 7 дней за запрос;
- лимит — 1 запрос в секунду.
Базовый адрес и имена параметров авторизации сверьте с письмом Callibri (CALLIBRI_BASE_URL в .env).
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Touch
from ..pipeline.normalize import normalize_phone, parse_dt
from .base import Collector

BASE_URL = os.environ.get("CALLIBRI_BASE_URL", "https://api.callibri.ru/")
MISSED_WORDS = ("пропущ", "missed", "не отвеч", "no answer", "noanswer", "busy", "занят")


class CallibriClient:
    def __init__(self, email: str, token: str, http: httpx.Client | None = None, sleep=time.sleep):
        if not email or not token:
            raise ValueError("Не заданы CALLIBRI_EMAIL и CALLIBRI_TOKEN")
        self.auth = {"user_email": email, "user_token": token}
        self.http = http or httpx.Client(timeout=60)
        self.sleep = sleep

    def get(self, method: str, **params) -> dict:
        self.sleep(1.05)  # не чаще 1 запроса в секунду
        r = self.http.get(BASE_URL.rstrip("/") + "/" + method, params={**self.auth, **params})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Callibri: {data['error']}")
        return data

    def statistics(self, site_id: str, date_from: date, date_to: date) -> list[dict]:
        """Разбивает период на куски по 7 дней и возвращает список ответов."""
        out, d = [], date_from
        while d <= date_to:
            end = min(d + timedelta(days=6), date_to)
            out.append(self.get("site_get_statistics", site_id=site_id, date1=d.strftime("%d.%m.%Y"), date2=end.strftime("%d.%m.%Y")))
            d = end + timedelta(days=1)
        return out


def _answered(call: dict) -> bool | None:
    status = " ".join(str(call.get(k) or "") for k in ("call_status", "status")).lower()
    if any(w in status for w in MISSED_WORDS):
        return False
    duration = call.get("duration") or call.get("talk_duration")
    try:
        if duration is not None:
            return int(float(duration)) > 0
    except (TypeError, ValueError):
        pass
    return True if status else None


CHANNEL_KEYS = ("name_channel", "channel_name", "channel", "name")


def _channel_name(block: dict, item: dict) -> str:
    """Канал колтрекинга (подменный номер «Яндекс Директ», статичный номер «2ГИС» и т. п.)."""
    for src in (item, block):
        for k in CHANNEL_KEYS:
            if src is item and k == "name":  # name у обращения — имя клиента
                continue
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _touch(kind: str, item: dict, idx: int, channel: str = "") -> Touch | None:
    created = parse_dt(item.get("date"))
    if not created:
        return None
    ext = item.get("id") or f"{created.isoformat()}|{item.get('phone') or item.get('ip') or ''}|{idx}"
    duration = item.get("duration")
    try:
        duration = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return Touch(
        source={"calls": "call", "feedbacks": "form", "chats": "chat"}[kind],
        system="callibri",
        external_id=f"{kind}:{ext}",
        created_at=created,
        phone=normalize_phone(item.get("phone")),
        name=item.get("name") or None,
        utm_source=item.get("utm_source") or "",
        utm_medium=item.get("utm_medium") or "",
        utm_campaign=item.get("utm_campaign") or "",
        utm_content=item.get("utm_content") or "",
        utm_term=item.get("utm_term") or "",
        ym_client_id=str(item.get("metrika_client_id") or "") or None,
        duration_sec=duration,
        answered=_answered(item) if kind == "calls" else None,
        manager=item.get("responsible_manager") or None,
        extra={**{k: item.get(k) for k in ("form_name", "landing_page", "query", "region", "is_lid", "conversations_number",
                                           "traffic_type", "source") if item.get(k) is not None},
               **({"callibri_channel": channel} if channel else {})},
    )


def parse_statistics(payload: dict) -> list[Touch]:
    """Ответ site_get_statistics → обращения. Каналы могут прийти списком в channels_statistics."""
    blocks = payload.get("channels_statistics") if isinstance(payload.get("channels_statistics"), list) else [payload]
    touches, idx = [], 0
    for block in blocks:
        for kind in ("calls", "feedbacks", "chats"):
            for item in block.get(kind) or []:
                idx += 1
                t = _touch(kind, item, idx, _channel_name(block if block is not payload else {}, item))
                if t:
                    touches.append(t)
    return touches


class CallibriCollector(Collector):
    name = "callibri"

    def __init__(self, client: CallibriClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(self._client or (s.callibri_email and s.callibri_token and s.callibri_site_id))

    @property
    def client(self) -> CallibriClient:
        if self._client is None:
            s = get_settings()
            self._client = CallibriClient(s.callibri_email, s.callibri_token)
        return self._client

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        site_id = get_settings().callibri_site_id
        touches: dict[str, Touch] = {}
        for payload in self.client.statistics(site_id, date_from, date_to):
            for t in parse_statistics(payload):
                touches[t.external_id] = t
        start = date_from
        end = date_to + timedelta(days=1)
        from datetime import datetime as _dt

        session.execute(
            delete(Touch).where(
                Touch.system == "callibri",
                Touch.created_at >= _dt.combine(start, _dt.min.time()),
                Touch.created_at < _dt.combine(end, _dt.min.time()),
            )
        )
        # Ещё удаляем по ключам: если часовой пояс в ответе сдвинул обращение за край окна,
        # повторная вставка не упадёт на уникальном ключе.
        keys = list(touches)
        for i in range(0, len(keys), 500):
            session.execute(delete(Touch).where(Touch.system == "callibri", Touch.external_id.in_(keys[i : i + 500])))
        session.add_all(touches.values())
        return len(touches)
