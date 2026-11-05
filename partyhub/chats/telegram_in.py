"""Заявки из Telegram: бот платформы сидит в чате с заявками и читает новые сообщения.

Два способа получать сообщения:
- вебхук /api/telegram — нужен публичный HTTPS-адрес (тот же, что для кнопок в отчёте);
- опрос `python -m partyhub telegram-poll` — работает без публичного адреса, например на Маке.
  Опрос сам обрабатывает и кнопки «Применить» / «Отклонить».
Бот видит только сообщения после того, как его добавили в чат: истории Telegram боту не отдаёт.
Чтобы бот видел все сообщения группы, выключите ему режим приватности (@BotFather → /setprivacy → Disable)
или сделайте его администратором.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from ..config import get_settings
from ..db import session_scope
from .store import save_chat_message

MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=ZoneInfo(get_settings().timezone)).replace(tzinfo=None)


def leads_chat_id() -> str:
    return str(get_settings().telegram_leads_chat_id or "").strip()


def handle_lead_message(msg: dict) -> bool:
    """Сообщение из чата заявок → обращение. Другие чаты игнорируются. True — если что-то сохранено."""
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not leads_chat_id() or chat_id != leads_chat_id() or not msg.get("message_id"):
        return False
    text = msg.get("text") or msg.get("caption") or ""
    sender = msg.get("from") or {}
    sender_name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or None
    with session_scope() as s:
        touch = save_chat_message(
            s,
            system="telegram",
            external_id=f"tg:{chat_id}:{msg['message_id']}",
            created_at=_local(int(msg.get("date") or time.time())),
            text=text,
            sender_name=sender_name,
            extra={"chat_id": chat_id, "message_id": msg["message_id"]},
        )
        return touch is not None


def message_of(update: dict) -> dict | None:
    for key in MESSAGE_KEYS:
        if isinstance(update.get(key), dict):
            return update[key]
    return None


def poll_forever(on_saved=None, timeout: int = 50) -> None:
    """Долгий опрос getUpdates. Не работает, пока у бота стоит вебхук: Telegram отдаёт обновления только одним способом."""
    from ..reports.telegram import Telegram
    from ..web.app import _handle_callback

    tg = Telegram(http=httpx.Client(timeout=timeout + 20))
    if not tg.token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    hook = tg.call("getWebhookInfo", {}).get("url")
    if hook:
        raise SystemExit(f"У бота стоит вебхук {hook}. Либо снимите его (telegram-poll --drop-webhook), "
                         "либо не запускайте опрос: сообщения уже приходят на /api/telegram.")
    if not leads_chat_id():
        print("TELEGRAM_LEADS_CHAT_ID не задан: заявки не читаются, обрабатываются только кнопки отчёта", flush=True)
    offset = None
    print("Читаю Telegram…", flush=True)
    while True:
        try:
            payload = {"timeout": timeout, "allowed_updates": ["message", "edited_message", "channel_post",
                                                                "edited_channel_post", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = tg.call("getUpdates", payload)
        except Exception as e:  # сеть моргнула — пробуем снова
            print(f"Telegram: {type(e).__name__}: {e}", flush=True)
            time.sleep(5)
            continue
        saved = 0
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if (u.get("callback_query") or {}).get("id"):
                    _handle_callback(u["callback_query"])
                elif (msg := message_of(u)) is not None:
                    saved += handle_lead_message(msg)
            except Exception as e:  # одно кривое сообщение не останавливает опрос
                print(f"Telegram: не обработано обновление {u.get('update_id')}: {type(e).__name__}: {e}", flush=True)
        if saved and on_saved:
            on_saved()


def chat_list_hint() -> list[dict]:
    """Чаты, из которых боту недавно писали: так проще узнать TELEGRAM_LEADS_CHAT_ID (только без вебхука)."""
    from ..reports.telegram import Telegram

    seen: dict[str, dict] = {}
    for u in Telegram().call("getUpdates", {"timeout": 0}):
        msg = message_of(u) or ((u.get("my_chat_member") or {}) if u.get("my_chat_member") else None)
        chat = (msg or {}).get("chat") or {}
        if chat.get("id") is not None:
            seen[str(chat["id"])] = {"id": chat["id"], "type": chat.get("type"), "title": chat.get("title") or chat.get("username")}
    return list(seen.values())
