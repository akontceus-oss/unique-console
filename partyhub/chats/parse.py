"""Разбор текста заявки из чата: телефон, имя, UTM-метки, комментарий.

Подходит и для шаблонных уведомлений («Имя: Анна\nТелефон: 091 12-34-56\nutm_source: yandex»),
и для свободного текста («Анна, 091123456, день рождения в субботу»). Нейросеть не используется:
телефоны и имена клиентов никуда не уходят.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

import phonenumbers

from ..config import get_settings
from ..pipeline.normalize import normalize_phone

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
LABELS = {
    "name": ("имя", "имя клиента", "клиент", "фио", "name", "контакт", "контактное лицо"),
    "phone": ("телефон", "тел", "тел.", "phone", "номер", "номер телефона", "мобильный", "whatsapp"),
    "comment": ("комментарий", "comment", "сообщение", "message", "вопрос", "пожелания", "текст"),
}
LABEL_OF = {alias: key for key, aliases in LABELS.items() for alias in aliases}

LINE_RE = re.compile(r"^\s*[*_•\-]*\s*([^:=\n]{1,40}?)\s*[*_]*\s*[:=]\s*(.+?)\s*$")
PHONE_RE = re.compile(r"(?<![\d.])\+?\d[\d \-()]{6,18}\d(?![\d.])")
UTM_RE = re.compile(r"\b(utm_(?:source|medium|campaign|content|term))\s*[=:]\s*([^&\s]+)", re.I)
URL_RE = re.compile(r"https?://\S+")


@dataclass
class ParsedMessage:
    phone: str | None = None
    name: str | None = None
    comment: str = ""
    utm: dict[str, str] = field(default_factory=dict)
    labeled: bool = False  # в тексте были поля «Имя:», «Телефон:» или метки — похоже на заявку

    @property
    def looks_like_lead(self) -> bool:
        return bool(self.phone or self.labeled)


def _valid_phone(raw: str, strict: bool) -> str | None:
    """Свободный текст — только номера, которые phonenumbers считает настоящими: даты и суммы не пройдут."""
    digits = re.sub(r"[^\d+]", "", raw)
    if not strict:
        return normalize_phone(digits)
    try:
        parsed = phonenumbers.parse(digits, get_settings().phone_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def parse_message(text: str | None) -> ParsedMessage:
    out = ParsedMessage()
    text = (text or "").strip()
    if not text:
        return out

    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        label, value = m.group(1).strip().lower(), m.group(2).strip(" *_")
        key = LABEL_OF.get(label)
        if label in UTM_KEYS:
            out.utm.setdefault(label, value)
            out.labeled = True
        elif key == "phone" and not out.phone:
            out.phone = _valid_phone(value, strict=False)
            out.labeled = True
        elif key == "name" and not out.name:
            out.name = value[:255]
            out.labeled = True
        elif key == "comment" and not out.comment:
            out.comment = value

    for url in URL_RE.findall(text):
        for k, v in parse_qsl(urlsplit(url).query):
            if k.lower() in UTM_KEYS and v:
                out.utm.setdefault(k.lower(), v)
    for k, v in UTM_RE.findall(text):
        out.utm.setdefault(k.lower(), v)
    if out.utm:
        out.labeled = True

    if not out.phone:
        for cand in PHONE_RE.findall(URL_RE.sub(" ", text)):
            out.phone = _valid_phone(cand, strict=True)
            if out.phone:
                break

    out.comment = (out.comment or text)[:1000]
    return out
