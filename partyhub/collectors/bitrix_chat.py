"""Чат Битрикс24 с заявками: сообщения → обращения типа «чат».

Метод im.dialog.messages.get, тот же входящий вебхук, что для CRM, с правом im.
Вебхук действует от имени сотрудника, который его создал: он должен состоять в этом чате.
Сбор идёт от последнего прочитанного сообщения вперёд; если запросили период раньше уже
прочитанного (collect --from), сборщик дочитывает историю назад до начала периода.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..chats.store import save_chat_message
from ..config import get_settings
from ..models import SyncCursor, Touch
from ..pipeline.normalize import parse_dt
from .base import Collector
from .bitrix import BitrixClient

PAGE = 50
BBCODE_RE = re.compile(r"\[/?[a-zA-Z*]+(?:=[^\]]*)?\]")


def dialog_id(value: str) -> str:
    value = (value or "").strip()
    return value if value.lower().startswith("chat") else f"chat{value}"


def clean_text(text: str | None) -> str:
    """BB-коды Битрикса ([b], [url=…], [br]) → обычный текст. Ссылки сохраняются: в них бывают UTM-метки."""
    text = (text or "").replace("[br]", "\n").replace("[BR]", "\n")
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"\2 \1", text, flags=re.I | re.S)
    return BBCODE_RE.sub("", text).strip()


def _cursor_get(session: Session, dialog: str) -> int | None:
    row = session.get(SyncCursor, f"bitrix_chat:{dialog}")
    return int(row.value) if row and row.value.isdigit() else None


def _cursor_set(session: Session, dialog: str, value: int) -> None:
    row = session.get(SyncCursor, f"bitrix_chat:{dialog}")
    if row is None:
        session.add(SyncCursor(key=f"bitrix_chat:{dialog}", value=str(value)))
    else:
        row.value = str(max(value, int(row.value) if row.value.isdigit() else 0))


class BitrixChatCollector(Collector):
    name = "bitrix_chat"

    def __init__(self, client: BitrixClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(s.bitrix_chat_id and (self._client or s.bitrix_webhook_url))

    @property
    def client(self) -> BitrixClient:
        if self._client is None:
            self._client = BitrixClient(get_settings().bitrix_webhook_url)
        return self._client

    def _page(self, dialog: str, **cursor) -> tuple[list[dict], dict[str, str]]:
        res = self.client.call("im.dialog.messages.get", {"DIALOG_ID": dialog, "LIMIT": PAGE, **cursor}).get("result") or {}
        users = {str(u.get("id")): u.get("name") or "" for u in res.get("users") or []}
        return list(res.get("messages") or []), users

    def _known(self, session: Session, dialog: str) -> tuple[int | None, int | None, datetime | None]:
        prefix = f"bxchat:{dialog}:"
        rows = session.execute(select(Touch.external_id, Touch.created_at).where(
            Touch.system == "bitrix_chat", Touch.external_id.like(prefix + "%"))).all()
        ids = [int(e.rsplit(":", 1)[1]) for e, _ in rows if e.rsplit(":", 1)[1].isdigit()]
        oldest = min((c for _, c in rows), default=None)
        return (max(ids) if ids else None), (min(ids) if ids else None), oldest

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        dialog = dialog_id(get_settings().bitrix_chat_id)
        start = datetime.combine(date_from, datetime.min.time())
        newest, oldest_id, oldest_at = self._known(session, dialog)
        # Сообщения без заявки не сохраняются, поэтому «последнее прочитанное» храним отдельно.
        newest = max(filter(None, (newest, _cursor_get(session, dialog))), default=None)
        n = 0
        seen_max = newest

        def save(messages: list[dict], users: dict[str, str]) -> None:
            nonlocal n, seen_max
            for m in messages:
                mid = int(m.get("id") or 0)
                seen_max = max(seen_max or 0, mid)
                if not mid or not int(m.get("author_id") or 0):  # системные сообщения чата
                    continue
                created = parse_dt(m.get("date"))
                if created is None:
                    continue
                touch = save_chat_message(
                    session, system="bitrix_chat", external_id=f"bxchat:{dialog}:{mid}", created_at=created,
                    text=clean_text(m.get("text")), sender_name=users.get(str(m.get("author_id"))),
                    extra={"dialog_id": dialog, "message_id": mid},
                )
                n += touch is not None

        if newest is None:
            # Первый запуск: читаем назад от последнего сообщения до начала периода.
            self._backwards(dialog, None, start, save)
        else:
            cursor = newest
            for _ in range(200):
                messages, users = self._page(dialog, FIRST_ID=cursor)
                save(messages, users)
                ids = [int(m.get("id") or 0) for m in messages]
                if len(messages) < PAGE or not ids or max(ids) <= cursor:
                    break
                cursor = max(ids)
            if oldest_id and oldest_at and oldest_at > start:
                self._backwards(dialog, oldest_id, start, save)
        if seen_max:
            _cursor_set(session, dialog, seen_max)
        session.flush()
        return n

    def _backwards(self, dialog: str, before: int | None, start: datetime, save) -> None:
        cursor = before
        for _ in range(200):
            messages, users = self._page(dialog, **({"LAST_ID": cursor} if cursor else {}))
            if not messages:
                break
            save([m for m in messages if (parse_dt(m.get("date")) or start) >= start], users)
            ids = [int(m.get("id") or 0) for m in messages]
            dates = [d for d in (parse_dt(m.get("date")) for m in messages) if d]
            if len(messages) < PAGE or (dates and min(dates) < start) or (cursor and min(ids) >= cursor):
                break
            cursor = min(ids)
