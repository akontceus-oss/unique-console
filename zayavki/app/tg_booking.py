"""Оповещение о брони в Telegram: предоплата отмечена и заполнены дата, время, комната.

Прежнее сообщение о брони удаляется (или зачёркивается), так что живых сообщений в чате столько же, сколько броней.

Что уже отправлено, хранится в lead.booking_sent (JSON). Поменяли бронь — уходит «Бронь изменена»
с прежними значениями; сняли предоплату, удалили бронь или заявку — «Бронь отменена».
Бот и чат — в .env сервера: ZAYAVKI_TG_BOT_TOKEN, ZAYAVKI_TG_BOOKING_CHAT. Нет их — ничего не шлём.
"""
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime
from html import escape

from .models import BOOKED_STATUS, Lead, mixed_package
from .phones import format_phone

log = logging.getLogger(__name__)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
# поля сообщения: ключ → подпись
LINES = [("client", "Заказчик"), ("phone", "Номер"), ("date", "Дата"), ("time", "Время"), ("room", "Комната"),
         ("show", "Шоу"), ("package", "Пакет"), ("prepaid", "Предоплата")]
KEYS = ("date", "time", "room", "show", "package", "prepaid")   # их изменение — повод для «Бронь изменена»


def _day(d: date | datetime | None) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}" if d else "—"


def _time(t: str, hours: int = 3) -> str:
    try:
        h, m = map(int, t.split(":"))
        return f"{t}–{(h + hours) % 24:02d}:{m:02d}" + (f" ({hours} ч)" if hours != 3 else "")
    except ValueError:
        return t or "—"


def snapshot(lead: Lead) -> dict | None:
    """Что должно быть в сообщении; None — брони нет (нет предоплаты, даты, времени или комнаты)."""
    if lead.status != BOOKED_STATUS or not (lead.booking_date and lead.booking_time and lead.booking_room):
        return None
    paid = f"Оплачено ✅ {lead.prepaid_amount:,} ₽".replace(",", " ") if lead.prepaid_amount else "Оплачено ✅"
    return {"client": lead.name or "—", "phone": format_phone(lead.phone_norm) or lead.phone or "—",
            "date": _day(lead.booking_date), "time": _time(lead.booking_time, lead.booking_hours or 3), "room": lead.booking_room,
            "show": lead.booking_show or "выбирают", "prepaid": paid,
            "package": (lead.package + " (согласовано с Татьяной)" if mixed_package(lead.package) else lead.package) or "—"}


def message(lead: Lead, snap: dict, old: dict | None) -> str:
    head = "✏️ Бронь изменена" if old else "🎉 Бронь"
    rows = [f"<b>{head}</b>", f"Менеджер: {escape(lead.manager.name if lead.manager else '—')}",
            f"Бронь: заявка от {_day(lead.created_at)}"]
    for k, label in LINES:
        v = escape(str(snap.get(k, "—")))
        if old and old.get(k) != snap.get(k):
            rows.append(f"<b>{label}: {v}</b> (было: {escape(str(old.get(k, '—')))})")
        else:
            rows.append(f"{label}: {v}")
    return "\n".join(rows)


def _api(method: str, **params):
    token = os.environ.get("ZAYAVKI_TG_BOT_TOKEN", "")
    data = urllib.parse.urlencode({**params, "chat_id": os.environ.get("ZAYAVKI_TG_BOOKING_CHAT", "")}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=15) as r:
        return json.loads(r.read())["result"]


def _post(lead_id: int, text: str | None, old_id: int | None, old_text: str | None, cancel: bool) -> None:
    """В отдельном потоке: старое сообщение удалить (бот может только в первые 48 часов — иначе
    зачеркнуть), новое отправить и запомнить его id. Отмена — старое зачёркивается, нового нет."""
    try:
        if old_id:
            done = False
            if not cancel:
                try:
                    done = bool(_api("deleteMessage", message_id=old_id))
                except Exception:
                    pass
            if not done:
                tail = "❌ <b>Бронь отменена</b>" if cancel else "↓ Бронь изменена — актуальная ниже"
                _api("editMessageText", message_id=old_id, parse_mode="HTML", text=f"<s>{old_text}</s>\n{tail}")
        new_id = _api("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true")["message_id"] if text else None
        from .db import session_scope
        with session_scope() as s:
            lead = s.get(Lead, lead_id)
            if lead:
                lead.booking_msg_id = new_id
    except Exception as e:                          # Telegram недоступен — не мешаем работе панели
        log.warning("Telegram: бронь не отправлена: %s", type(e).__name__)


def send(lead_id: int, text: str | None, old_id: int | None = None, old_text: str | None = None, cancel: bool = False) -> None:
    if not os.environ.get("ZAYAVKI_TG_BOT_TOKEN") or not os.environ.get("ZAYAVKI_TG_BOOKING_CHAT"):
        return
    threading.Thread(target=_post, args=(lead_id, text, old_id, old_text, cancel), daemon=True).start()


def sync(lead: Lead) -> None:
    """Вызывать после любых изменений заявки. В чате на каждую бронь одно живое сообщение:
    новая бронь — сообщение; изменили — старое удаляется, приходит «Бронь изменена»;
    сняли предоплату, удалили бронь или заявку — старое зачёркивается с пометкой «Бронь отменена»."""
    if not os.environ.get("ZAYAVKI_TG_BOT_TOKEN") or not os.environ.get("ZAYAVKI_TG_BOOKING_CHAT"):
        return                      # бот не настроен — ничего не помечаем, иначе бронь потом не уйдёт
    old = json.loads(lead.booking_sent) if lead.booking_sent else None
    snap = snapshot(lead)
    if snap == old:
        return
    if snap and old and all(snap[k] == old.get(k) for k in KEYS):   # поменяли только имя или телефон — молча
        lead.booking_sent = json.dumps(snap, ensure_ascii=False)
        return
    old_text = message(lead, old, None) if old else None
    if snap is None:
        send(lead.id, None, lead.booking_msg_id, old_text, cancel=True)
        lead.booking_sent = ""
    else:
        send(lead.id, message(lead, snap, old), lead.booking_msg_id, old_text)
        lead.booking_sent = json.dumps(snap, ensure_ascii=False)


def mark_existing(s) -> None:
    """Брони, сделанные до включения оповещений, считаем уже отправленными (иначе любая правка
    старой брони прислала бы её в чат). У них колонка пустая (NULL) только один раз — после обновления."""
    from sqlalchemy import select
    for lead in s.scalars(select(Lead).where(Lead.booking_sent.is_(None))):
        snap = snapshot(lead)
        lead.booking_sent = json.dumps(snap, ensure_ascii=False) if snap else ""
