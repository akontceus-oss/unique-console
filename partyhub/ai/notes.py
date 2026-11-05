"""ИИ-модуль: короткие наблюдения и гипотезы к ежедневному отчёту.

Модулю передаются только обезличенные цифры (расходы, клики, площадки, кампании) —
ни телефонов, ни имён. Провайдер выбирается в .env (AI_PROVIDER): template (без ИИ, по умолчанию),
openai, gemini, anthropic, yandexgpt — см. ai/providers.py.
"""
from __future__ import annotations

import json

import httpx

from ..config import get_settings
from .providers import get_provider

SYSTEM = (
    "Ты — аналитик контекстной рекламы детского развлекательного центра. Цель рекламы — брони детских дней рождения. "
    "Тебе дают обезличенные цифры за вчера и за 30 дней. Напиши 2–4 коротких наблюдения на русском: что изменилось, "
    "что вызывает вопрос, и одну гипотезу для эксперимента. Опирайся только на переданные цифры, не выдумывай данных. "
    "Без вступлений, каждый пункт с новой строки, начиная с «— »."
)


def facts_for_ai(summary: dict, campaigns: list[dict], placements: list[dict]) -> dict:
    """Только агрегаты — без персональных данных."""
    t = summary["totals"]
    return {
        "period": summary["period"],
        "totals": {k: t.get(k) for k in ("cost", "leads_direct", "bookings_direct", "cpl", "cpb", "lead_to_booking", "romi")},
        "change_vs_previous": {k: summary["change"].get(k) for k in ("cost", "leads_direct", "bookings_direct", "cpl", "cpb")},
        "targets": summary["targets"],
        "campaigns": [
            {k: c.get(k) for k in ("campaign", "type", "cost", "leads", "bookings", "cpl", "cpb", "cost_share", "bookings_share")}
            for c in campaigns
        ],
        "top_placements": [
            {k: p.get(k) for k in ("placement", "cost", "clicks", "leads", "bookings", "bounce_rate", "status")}
            for p in placements[:15]
        ],
    }


def generate_notes(facts: dict, http: httpx.Client | None = None) -> str | None:
    provider = (get_settings().ai_provider or "template").lower()
    if provider == "template":
        return None
    user = "Цифры (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str)
    try:
        return get_provider(provider, http).complete(SYSTEM, user, max_tokens=700)
    except Exception as e:  # отчёт уходит и без ИИ
        return f"(ИИ-модуль недоступен: {type(e).__name__})"
