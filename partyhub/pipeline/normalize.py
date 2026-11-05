"""Нормализация телефонов, UTM-меток и времени."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import phonenumbers

from ..config import get_settings

# Рекомендуемая разметка ссылок в Директе:
#   utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}
#   &utm_content={source_type}|{source}&utm_term={keyword}
# {source_type} = search | context, {source} = площадка РСЯ (на поиске — none).
SEARCH_MARKERS = {"", "none", "search", "yandex.ru", "ya.ru"}


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Приводит телефон к E.164 (+37491234567). Пустой или нераспознанный → None."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if len(re.sub(r"\D", "", digits)) < 6:
        return None
    region = region or get_settings().phone_region
    try:
        parsed = phonenumbers.parse(digits, region)
        if phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return "+" + re.sub(r"\D", "", digits)


def mask_phone(phone: str | None) -> str:
    """+37491234567 → +374 9•••••67 — для экранов, где полный номер не нужен."""
    if not isinstance(phone, str) or not phone:
        return "—"
    d = re.sub(r"\D", "", phone)
    if len(d) < 6:
        return "•••"
    return f"+{d[:3]} {d[3]}{'•' * (len(d) - 6)}{d[-2:]}"


def parse_utm_content(value: str | None) -> tuple[str | None, str | None]:
    """'context|games.example' → ('AD_NETWORK', 'games.example'); 'search|none' → ('SEARCH', None)."""
    if not value:
        return None, None
    value = value.strip()
    source_type, _, source = value.partition("|") if "|" in value else ("", "", value)
    source_type = source_type.lower()
    source = source.strip().lower()
    if source_type == "search" or (not source_type and source in SEARCH_MARKERS):
        return "SEARCH", None
    if source_type == "context" or source:
        return "AD_NETWORK", source or None
    return None, None


def parse_campaign_id(value: str | None) -> int | None:
    if value and str(value).strip().isdigit():
        return int(str(value).strip())
    return None


def is_yandex_ads(utm_source: str | None, utm_medium: str | None) -> bool:
    return (utm_source or "").lower() in {"yandex", "yandex.direct", "direct"} and (utm_medium or "").lower() in {
        "cpc",
        "ppc",
        "paid",
        "",
    }


def source_label(utm_source: str, utm_medium: str, network: str | None) -> str:
    s, m = (utm_source or "").lower(), (utm_medium or "").lower()
    if is_yandex_ads(s, m) and s:
        return "Директ · РСЯ" if network == "AD_NETWORK" else "Директ · Поиск"
    if m in {"cpc", "ppc", "paid"}:
        return "Другая реклама"
    if m == "organic" or s in {"google", "yandex"}:
        return "SEO"
    if s in {"instagram", "facebook", "telegram", "vk"} or m in {"social", "smm"}:
        return "Соцсети"
    if not s:
        return "Прямые / неизвестно"
    return "Другое"


# ------------------------------------------------------------ время ---
def now_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)


def today_local() -> date:
    return now_local().date()


def yesterday() -> date:
    return today_local() - timedelta(days=1)


def to_local_naive(dt: datetime) -> datetime:
    """Переводит aware-время в локальное время бизнеса без tzinfo."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)


def parse_dt(value: str | None) -> datetime | None:
    """Разбирает ISO-время и форматы вида '15.01.2024 10:20:30'."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return to_local_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
