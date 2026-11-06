"""Источник заявки для сводки «Заявки и брони по источникам».

Источник определяется тремя способами, по убыванию надёжности (так решил Андрей 06.11.2026):
1. колтрекинг: канал, к которому Callibri отнёс звонок или заявку (подменный номер Директа,
   статичный номер в 2ГИС, в профиле Instagram и т. п.);
2. UTM-метки обращения (ссылка рекламы, форма сайта);
3. тег, который администратор поставил в панели заявок («Как узнали»).

Срабатывает первый способ, который узнал источник. Если ни один не узнал — «Без источника».
Сами названия источников — CHANNELS; порядок списка — порядок строк на дашборде.
"""
from __future__ import annotations

import re

DIRECT = "Яндекс Директ"
INSTAGRAM = "Instagram"
SEARCH = "Поиск"
MAPS = "Яндекс Карты"
GIS = "2ГИС"
OZON = "Ozon реклама"
FRIENDS = "Были на празднике у друзей"
VK = "ВКонтакте"
RETURNING = "Уже знают нас"
SITE = "Сайт"
OTHER_ADS = "Другая реклама"
OTHER = "Другое"
NONE = "Без источника"

CHANNELS = [DIRECT, INSTAGRAM, SEARCH, MAPS, GIS, OZON, FRIENDS, VK, RETURNING, SITE, OTHER_ADS, OTHER, NONE]

METHOD_UTM, METHOD_CALL, METHOD_TAG = "utm", "call", "tag"
METHOD_LABELS = {METHOD_UTM: "UTM", METHOD_CALL: "колтрекинг", METHOD_TAG: "тег"}

# Слова в названии канала колтрекинга или тега → источник. Проверяются по порядку,
# поэтому частные случаи («Яндекс Карты», «Яндекс Директ») стоят раньше общих («Яндекс»).
_WORDS: list[tuple[str, tuple[str, ...]]] = [
    (OZON, ("ozon", "озон")),
    (GIS, ("2gis", "2гис", "2 гис", "дубльгис", "dublgis")),
    (INSTAGRAM, ("instagram", "инстаграм", "инстаграмм", "insta", "инста", "meta", "facebook", "фейсбук")),
    (MAPS, ("яндекс карт", "yandex map", "yandex_map", "yandexmap", "я.карт", "карты", "maps")),
    (DIRECT, ("директ", "direct", "реклама яндекс", "яндекс реклам", "yandex ads", "рся", "yandex.direct")),
    (FRIENDS, ("празднике у друзей", "чужом празднике", "у друзей", "друз", "подруг", "знакомы", "сарафан", "рекомендац",
               "посоветовал")),
    (RETURNING, ("приходили раньше", "были у нас", "постоян", "повторн", "вывеск", "живут рядом", "мимо")),
    (VK, ("вконтакт", "vkontakte", "vk.com", "вк ", "vk ")),
    (SEARCH, ("органик", "organic", "поиск", "search", "seo", "google", "гугл", "интернет")),
    (SITE, ("сайт", "site", "tilda", "тильд")),
    (OTHER_ADS, ("реклам", "biglion", "биглион", "cpc", "ppc", "таргет")),
]

# utm_source → источник (без учёта utm_medium).
_UTM_SOURCES = {
    "instagram": INSTAGRAM, "ig": INSTAGRAM, "insta": INSTAGRAM, "facebook": INSTAGRAM, "fb": INSTAGRAM, "meta": INSTAGRAM,
    "2gis": GIS, "dgis": GIS, "2гис": GIS,
    "ozon": OZON, "ozon_ads": OZON, "ozonads": OZON,
    "vk": VK, "vkontakte": VK, "vk.com": VK, "vkads": VK,
    "yandex_maps": MAPS, "yandexmaps": MAPS, "ymaps": MAPS, "maps": MAPS,
    "google": SEARCH, "bing": SEARCH,
    "biglion": OTHER_ADS,
}
_PAID = {"cpc", "ppc", "paid", "cpm", "cpa", "ads", "context", "smm_paid", "target", "targeting"}
_YANDEX = {"yandex", "yandex.direct", "direct", "yandex_direct", "ya", "yd"}


def from_words(text: str | None) -> str | None:
    """Название канала колтрекинга или тег панели → источник; None, если не узнали."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")) + " "
    if not t.strip():
        return None
    if t.strip() in {"другое", "other", "не указан", "неизвестно", "-", "—"}:
        return OTHER
    for channel, words in _WORDS:
        if any(w in t for w in words):
            return channel
    return None


def from_utm(utm_source: str | None, utm_medium: str | None) -> str | None:
    s = (utm_source or "").strip().lower()
    m = (utm_medium or "").strip().lower()
    if not s and not m:
        return None
    if s in _YANDEX:
        if m in {"organic", "search"}:
            return SEARCH
        if m in {"maps", "map", "yandex_maps"}:
            return MAPS
        return DIRECT  # yandex без medium или с cpc — разметка Директа
    if s in _UTM_SOURCES:
        return _UTM_SOURCES[s]
    if m == "organic":
        return SEARCH
    by_words = from_words(s)
    if by_words and by_words not in (SITE, OTHER):
        return by_words
    if m in _PAID:
        return OTHER_ADS
    if m in {"social", "smm"}:
        return from_words(s) or OTHER
    return OTHER


def callibri_channel(extra: dict | None) -> str:
    """Название канала, к которому Callibri отнёс обращение (кладётся в Touch.extra сборщиком)."""
    extra = extra or {}
    return str(extra.get("callibri_channel") or extra.get("traffic_type") or "")


def classify(touches, tag: str | None = None) -> tuple[str, str]:
    """Обращения заявки (в порядке времени) и тег панели → (источник, способ).

    touches — объекты с полями utm_source, utm_medium, system, extra.
    """
    # колтрекинг точнее всего: номер жёстко привязан к каналу
    for t in touches:
        if t.system == "callibri":
            ch = from_words(callibri_channel(t.extra))
            if ch and ch != OTHER:
                return ch, METHOD_CALL
    unknown_utm = False  # метки есть, но непонятно чьи: уступаем тегу
    for t in touches:
        ch = from_utm(t.utm_source, t.utm_medium)
        if ch == OTHER:
            unknown_utm = True
        elif ch:
            return ch, METHOD_UTM
    tags = [tag] if tag else []
    for t in touches:
        if t.system == "zayavki":
            extra = t.extra or {}
            tags += [extra.get("tag"), extra.get("tag_group")]
    for value in tags:
        ch = from_words(value)
        if ch:
            return ch, METHOD_TAG
    return (OTHER, METHOD_UTM) if unknown_utm else (NONE, "")


def split_tag(value: str | None) -> tuple[str, str]:
    """«Знакомые · Друзья, подруга» → ('Друзья, подруга', 'Знакомые'): название канала важнее группы."""
    value = (value or "").strip()
    if " · " in value:
        group, _, name = value.partition(" · ")
        return name.strip(), group.strip()
    return value, ""
