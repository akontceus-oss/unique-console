"""Сообщения из чатов (Telegram, чат Битрикс24) → обращения типа «чат»."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_rules
from ..models import Touch
from .parse import UTM_KEYS, parse_message


def chat_rules() -> dict:
    return {"keep_without_phone": True, "sender_is_client": False, **(load_rules().get("chats") or {})}


def save_chat_message(session: Session, system: str, external_id: str, created_at: datetime, text: str | None,
                      sender_name: str | None = None, extra: dict | None = None) -> Touch | None:
    """Сохраняет сообщение как обращение. Повтор того же сообщения (или его правка) обновляет запись.

    Не похожее на заявку сообщение (нет телефона, полей «Имя:» и меток) пропускается: обычная переписка
    менеджеров в таблицу не попадает. Сообщение без телефона, но с полями заявки сохраняется,
    если в rules.yaml включено chats.keep_without_phone.
    """
    rules = chat_rules()
    parsed = parse_message(text)
    touch = session.scalar(select(Touch).where(Touch.system == system, Touch.external_id == external_id))
    keep = parsed.phone or (parsed.looks_like_lead and rules["keep_without_phone"])
    if not keep:
        if touch is not None:  # сообщение отредактировали, и из него пропал телефон
            session.delete(touch)
        return None
    if touch is None:
        touch = Touch(source="chat", system=system, external_id=external_id, created_at=created_at)
        session.add(touch)
    touch.phone = parsed.phone
    touch.name = parsed.name or (sender_name if rules["sender_is_client"] else None)
    for k in UTM_KEYS:
        setattr(touch, k, parsed.utm.get(k, "")[:255 if k in ("utm_source", "utm_medium", "utm_campaign") else 500])
    touch.extra = {**(extra or {}), "comment": parsed.comment}
    session.flush()
    return touch
