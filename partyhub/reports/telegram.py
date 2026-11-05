"""Telegram: отправка отчёта с кнопками и обработка нажатий «Применить» / «Отклонить»."""
from __future__ import annotations

import httpx

from ..config import get_settings

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None, http: httpx.Client | None = None):
        s = get_settings()
        self.token = token or s.telegram_bot_token
        self.chat_id = chat_id or s.telegram_chat_id
        self.http = http or httpx.Client(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def call(self, method: str, payload: dict) -> dict:
        r = self.http.post(API.format(token=self.token, method=method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> dict:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    def edit_buttons(self, chat_id, message_id, buttons: list[list[dict]] | None) -> None:
        self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": buttons or []}},
        )

    def set_webhook(self, url: str, secret: str) -> dict:
        return self.call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["callback_query", "message"]})


def action_buttons(action_ids: list[int]) -> list[list[dict]]:
    rows = []
    for i, aid in enumerate(action_ids, 1):
        rows.append(
            [
                {"text": f"Применить {i}", "callback_data": f"ap:{aid}"},
                {"text": f"Отклонить {i}", "callback_data": f"rj:{aid}"},
            ]
        )
    return rows


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram принимает до 4096 символов — режем по абзацам."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        candidate = (cur + "\n\n" + block) if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            parts.append(cur)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        cur = block
    if cur:
        parts.append(cur)
    return parts
