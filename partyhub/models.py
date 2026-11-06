"""Таблицы платформы.

Время хранится как «наивное» локальное время бизнеса (settings.timezone):
так проще считать «вчера», «за 7 дней» и строить отчёты.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# ---------------------------------------------------------------- Реклама ---
class AdStat(Base):
    """Дневная статистика Директа: кампания × группа × тип сети × площадка."""

    __tablename__ = "ad_stats_daily"
    __table_args__ = (
        UniqueConstraint("date", "platform", "campaign_id", "ad_group_id", "network", "placement", name="uq_ad_stat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    platform: Mapped[str] = mapped_column(String(20), default="direct")
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    ad_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ad_group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network: Mapped[str] = mapped_column(String(20))  # SEARCH | AD_NETWORK
    placement: Mapped[str] = mapped_column(String(255), index=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)


class SearchQuery(Base):
    """Поисковые запросы, по которым показывались объявления."""

    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    ad_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    query: Mapped[str] = mapped_column(String(500))
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    # Чем подобран запрос: KEYWORD — ключевая фраза, AUTOTARGETING — автотаргетинг (None — старые данные).
    criterion_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    criterion: Mapped[str | None] = mapped_column(String(500), nullable=True)
    targeting_category: Mapped[str | None] = mapped_column(String(30), nullable=True)  # категория автотаргетинга
    conversions: Mapped[int | None] = mapped_column(Integer, nullable=True)  # по целям DIRECT_GOAL_IDS


class DirectSliceStat(Base):
    """Срезы статистики Директа для корректировок ставок: устройство, пол × возраст.

    slice: device | demography; value: MOBILE | GENDER_FEMALE|AGE_25_34 и т. п. (значения API Директа).
    Конверсии — по целям DIRECT_GOAL_IDS, как их считает сам Директ.
    """

    __tablename__ = "direct_slice_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    slice: Mapped[str] = mapped_column(String(20), index=True)
    value: Mapped[str] = mapped_column(String(64))
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)


class WebStat(Base):
    """Визиты из Метрики по UTM-меткам. Хранятся суммы, а не доли, чтобы их можно было складывать."""

    __tablename__ = "web_stats_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    utm_source: Mapped[str] = mapped_column(String(255), default="")
    utm_medium: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(255), default="")
    utm_content: Mapped[str] = mapped_column(String(500), default="")
    utm_term: Mapped[str] = mapped_column(String(500), default="")
    placement: Mapped[str] = mapped_column(String(255), default="", index=True)  # разобрано из utm_content
    visits: Mapped[int] = mapped_column(Integer, default=0)
    bounces: Mapped[int] = mapped_column(Integer, default=0)
    duration_sum: Mapped[int] = mapped_column(Integer, default=0)  # секунды, сумма по визитам
    goal_reaches: Mapped[int] = mapped_column(Integer, default=0)  # достижения основной цели «заявка»
    goals: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"id цели": достижения} по всем целям сайта


# ------------------------------------------------------------- Обращения ---
class Touch(Base):
    """Сырое обращение: звонок, заявка с формы, чат. Из них собираются заявки (Lead)."""

    __tablename__ = "touches"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_touch_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30), index=True)  # call | form | chat | site_form | panel
    system: Mapped[str] = mapped_column(String(30), default="")  # callibri | site | bitrix | zayavki
    external_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utm_source: Mapped[str] = mapped_column(String(255), default="")
    utm_medium: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(255), default="")
    utm_content: Mapped[str] = mapped_column(String(500), default="")
    utm_term: Mapped[str] = mapped_column(String(500), default="")
    ym_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    yclid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    response_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manager: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True)


class CrmRecord(Base):
    """Лид или сделка из CRM (Битрикс24) с нормализованным статусом."""

    __tablename__ = "crm_records"
    __table_args__ = (UniqueConstraint("entity", "external_id", name="uq_crm_entity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(10))  # lead | deal (Битрикс24) | booking (YCLIENTS) | panel (панель заявок)
    external_id: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_raw: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="new")
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manager: Mapped[str | None] = mapped_column(String(255), nullable=True)
    party_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    guests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lost_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utm_source: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(255), default="")
    utm_content: Mapped[str] = mapped_column(String(500), default="")


class Lead(Base):
    """Заявка = одно обращение клиента (все звонки/чаты с одного телефона в окне склейки)."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_touch_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_touch_at: Mapped[datetime] = mapped_column(DateTime)
    channel: Mapped[str] = mapped_column(String(30))  # первый канал: call | form | chat | site_form
    touches_count: Mapped[int] = mapped_column(Integer, default=1)
    source_label: Mapped[str] = mapped_column(String(50))  # «Директ · Поиск», «Директ · РСЯ», «SEO», …
    # Источник для сводки «Заявки и брони по источникам» (pipeline/channels.py) и чем он определён:
    # utm | call (колтрекинг) | tag (тег администратора в панели заявок) | "" (не определён)
    source_channel: Mapped[str] = mapped_column(String(40), default="")
    source_method: Mapped[str] = mapped_column(String(8), default="")
    utm_source: Mapped[str] = mapped_column(String(255), default="")
    utm_medium: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(255), default="")
    utm_content: Mapped[str] = mapped_column(String(500), default="")
    utm_term: Mapped[str] = mapped_column(String(500), default="")
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    network: Mapped[str | None] = mapped_column(String(20), nullable=True)
    placement: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    keyword: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    manager: Mapped[str | None] = mapped_column(String(255), nullable=True)
    party_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    guests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crm_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    missed_call: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------------------- SEO ---
class SeoPosition(Base):
    __tablename__ = "seo_positions"
    __table_args__ = (UniqueConstraint("date", "keyword", "region", name="uq_seo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    region: Mapped[str] = mapped_column(String(64), default="")  # «проект:индекс региона» Топвизора
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    region_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # «Ереван · Яндекс»
    project: Mapped[str | None] = mapped_column(String(200), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


class SeoKeyword(Base):
    """Частотность запроса из Топвизора: по ней видно, какие запросы важные."""

    __tablename__ = "seo_keywords"
    __table_args__ = (UniqueConstraint("project", "keyword", name="uq_seo_keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(200), default="")
    keyword: Mapped[str] = mapped_column(String(500))
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    volume_yandex: Mapped[int | None] = mapped_column(Integer, nullable=True)  # «"запрос"» — фразовая частота в месяц
    volume_google: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated: Mapped[date | None] = mapped_column(Date, nullable=True)


# --------------------------------------------- Настройки и журнал правок ---
class CampaignSnapshot(Base):
    """Ежедневный снимок настроек кампании — по разнице снимков видно все внешние правки."""

    __tablename__ = "campaign_snapshots"
    __table_args__ = (UniqueConstraint("date", "campaign_id", name="uq_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    data: Mapped[dict] = mapped_column(JSON)


class SettingsChange(Base):
    """Изменение настроек, найденное сравнением снимков (кто-то поменял не через платформу)."""

    __tablename__ = "settings_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(100))
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(String(30), default="external")  # external | platform


class Action(Base):
    """Правка, которую предложила или сделала платформа. Каждая — с причиной, цифрами и откатом."""

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    rule: Mapped[str] = mapped_column(String(50))
    level: Mapped[str] = mapped_column(String(20))  # auto | approval | alert
    kind: Mapped[str] = mapped_column(String(30))  # exclude_site | add_negative | bid_modifier | set_cpa | alert
    campaign_ids: Mapped[list] = mapped_column(JSON, default=list)
    campaign_names: Mapped[list] = mapped_column(JSON, default=list)
    target: Mapped[str] = mapped_column(String(500))  # площадка, минус-слово или кампания
    reason: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), index=True)
    # proposed | approved | applied | partial | rejected | rolled_back | failed | alert
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # dry_run | sandbox | live
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    eval_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ConversionUpload(Base):
    """Какие брони уже отправлены в Метрику как офлайн-конверсии — чтобы не слать повторно."""

    __tablename__ = "conversion_uploads"
    __table_args__ = (UniqueConstraint("lead_key", "target", name="uq_conversion"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_key: Mapped[str] = mapped_column(String(100))  # телефон или crm-id — стабилен между пересборками заявок
    target: Mapped[str] = mapped_column(String(100))
    client_id: Mapped[str] = mapped_column(String(64))
    id_type: Mapped[str] = mapped_column(String(20))  # CLIENT_ID | YCLID
    conversion_at: Mapped[datetime] = mapped_column(DateTime)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime)
    upload_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReportLog(Base):
    __tablename__ = "report_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    report_date: Mapped[date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CollectorRun(Base):
    """Журнал запусков сборщиков — чтобы видеть, какой источник не обновился."""

    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collector: Mapped[str] = mapped_column(String(30))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))  # ok | error | skipped
    rows: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)


# --------------------------------------------------------- ИИ-агент Директа ---
class DirectAgentRun(Base):
    """Задание ИИ-агенту: диалог с моделью, шаги для экрана, ответ и план изменений."""

    __tablename__ = "direct_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    task: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | error
    messages: Mapped[list] = mapped_column(JSON, default=list)  # диалог с моделью в общем формате ai/providers
    steps: Mapped[list] = mapped_column(JSON, default=list)  # что показать человеку: вызовы, таблицы, ответы
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    changeset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author: Mapped[str | None] = mapped_column(String(100), nullable=True)


class DirectChangeset(Base):
    """План изменений в Директе от агента. Применяется целиком только по решению человека."""

    __tablename__ = "direct_changesets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # draft | applying | applied | partial | failed | rejected | rolled_back
    ops: Mapped[list] = mapped_column(JSON, default=list)  # [{op, params, description, result, error, undo}]
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SyncCursor(Base):
    """Докуда прочитан источник (например, последний ID сообщения в чате Битрикс24)."""

    __tablename__ = "sync_cursors"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
