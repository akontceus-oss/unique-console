"""Настройки платформы.

Всё, что зависит от окружения (токены, адреса, режимы), читается из переменных
окружения или файла .env. Бизнес-правила (пороги, стоп-слова, статусы CRM) —
из config/rules.yaml, чтобы их можно было менять без правки кода.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- Общее ---
    database_url: str = f"sqlite:///{ROOT / 'partyhub.db'}"
    platform_database_url: str = ""  # реестр проектов; пусто — та же база, что DATABASE_URL
    timezone: str = "Asia/Yerevan"
    currency: str = "AMD"
    currency_symbol: str = "֏"
    phone_region: str = "AM"  # регион по умолчанию для разбора телефонов
    business_name: str = "Детский развлекательный центр"
    rules_file: str = str(ROOT / "config" / "rules.yaml")

    # --- Доступ к дашборду ---
    dashboard_user: str = "admin"
    dashboard_password: str = "change-me"
    webhook_token: str = "change-me-too"  # для приёма заявок с сайта

    # --- Яндекс Директ ---
    direct_token: str = ""
    direct_client_login: str = ""  # нужен только агентским аккаунтам
    direct_mode: str = "dry_run"  # dry_run | sandbox | live
    direct_goal_ids: str = ""  # id целей Метрики через запятую, для конверсий в отчётах

    # --- Яндекс Метрика ---
    metrika_token: str = ""
    metrika_counter_id: str = ""
    metrika_goal_id: str = ""  # основная цель «заявка»

    # --- Callibri ---
    callibri_email: str = ""
    callibri_token: str = ""
    callibri_site_id: str = ""

    # --- Битрикс24 ---
    bitrix_webhook_url: str = ""  # https://portal.bitrix24.xx/rest/1/xxxxxxxx/
    bitrix_party_date_field: str = ""  # UF_CRM_... — поле «дата праздника»
    bitrix_guests_field: str = ""  # UF_CRM_... — «количество детей»
    bitrix_chat_id: str = ""  # чат с заявками: chat123 или 123 (python -m partyhub bitrix-chats покажет список)

    # --- YCLIENTS (брони) ---
    yclients_partner_token: str = ""
    yclients_user_token: str = ""
    yclients_company_id: str = ""  # ID филиала

    # --- Топвизор ---
    topvisor_user_id: str = ""
    topvisor_api_key: str = ""
    topvisor_project_id: str = ""
    topvisor_region_index: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = "change-me-secret"
    telegram_leads_chat_id: str = ""  # чат, из которого бот читает заявки (может совпадать с TELEGRAM_CHAT_ID)
    public_base_url: str = ""  # https://hub.example.com — для ссылок в отчёте

    # --- Google-таблица заявок ---
    google_service_account_file: str = ""  # путь к JSON-ключу сервисного аккаунта
    google_sheet_id: str = ""  # из ссылки: docs.google.com/spreadsheets/d/<ID>/edit
    google_sheet_tab: str = "Заявки"

    # --- ИИ-модуль (необязательно) ---
    ai_provider: str = "template"  # для отчёта: template | openai | gemini | anthropic | yandexgpt
    direct_ai_provider: str = ""  # для вкладки «Директ»; пусто — как AI_PROVIDER
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"  # любой совместимый с OpenAI сервис
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    yandexgpt_api_key: str = ""
    yandexgpt_folder_id: str = ""
    yandexgpt_model: str = "yandexgpt/latest"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"

    # --- Расписание ---
    collect_time: str = "06:00"
    report_time: str = "09:00"
    leads_sync_minutes: int = 10  # как часто забирать новые заявки и обновлять таблицу; 0 — только утром

    @property
    def goal_ids(self) -> list[int]:
        return [int(x) for x in self.direct_goal_ids.split(",") if x.strip()]


@lru_cache
def base_settings() -> Settings:
    """Общие настройки платформы из .env — без учёта проекта."""
    return Settings()


_PROJECT_TTL = 30.0  # сек.: другой процесс (планировщик) увидит правки из кабинета не позже чем через полминуты
_project_cache: dict[str, tuple[float, Settings, dict]] = {}


def _project_entry(slug: str) -> tuple[Settings, dict]:
    import time

    from .projects import get_project, overrides, project_rules

    hit = _project_cache.get(slug)
    if hit and time.monotonic() - hit[0] < _PROJECT_TTL:
        return hit[1], hit[2]
    p = get_project(slug)
    if p is None:
        raise KeyError(f"Нет проекта «{slug}»")
    base = base_settings()
    values = {**base.model_dump(), **overrides(p)}
    # model_validate не перечитывает .env: значения проекта перекрывают общие
    s = Settings.model_validate({k: v for k, v in values.items() if k in Settings.model_fields})
    _project_cache[slug] = (time.monotonic(), s, project_rules(p))
    return s, _project_cache[slug][2]


def get_settings() -> Settings:
    """Настройки текущего проекта (внутри use_project) или общие из .env."""
    from .projects import current_slug

    slug = current_slug()
    return _project_entry(slug)[0] if slug else base_settings()


def clear_caches() -> None:
    base_settings.cache_clear()
    _project_cache.clear()
    _yaml_rules.cache_clear()


get_settings.cache_clear = clear_caches  # type: ignore[attr-defined]  совместимость: тесты и CLI сбрасывают кэш так


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache
def _yaml_rules(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rules(path: str | None = None) -> dict[str, Any]:
    """config/rules.yaml, поверх — правила проекта (целевые цены, лимиты агента, защищённые кампании)."""
    from .projects import current_slug

    rules = _yaml_rules(path or get_settings().rules_file)
    slug = current_slug()
    return _merge(rules, _project_entry(slug)[1]) if slug and not path else rules


load_rules.cache_clear = clear_caches  # type: ignore[attr-defined]
