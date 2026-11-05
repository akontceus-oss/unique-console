"""Проекты платформы: у каждого свои подключения, свои настройки и своя база.

Реестр проектов лежит в базе платформы (PLATFORM_DATABASE_URL, по умолчанию — та же, что DATABASE_URL).
Данные проекта — в отдельной базе: SQLite-файл data/projects/<slug>.db или схема p_<slug> в PostgreSQL.
Код сборщиков, правил и дашборда не знает о проектах: внутри `use_project(slug)` функция
get_settings() отдаёт настройки этого проекта, а session_scope() — его базу.

Ключи хранятся зашифрованными (PLATFORM_SECRET_KEY или файл secrets/platform.key).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import os
import re
import secrets as pysecrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import DateTime, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("partyhub_project", default=None)


class PlatformBase(DeclarativeBase):
    pass


class Project(PlatformBase):
    __tablename__ = "platform_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    settings_json: Mapped[str] = mapped_column(Text, default="{}")  # обычные поля
    secrets_enc: Mapped[str] = mapped_column(Text, default="")  # ключи и токены, зашифрованы
    rules_json: Mapped[str] = mapped_column(Text, default="{}")  # поверх config/rules.yaml
    version: Mapped[int] = mapped_column(Integer, default=1)  # растёт при каждом сохранении
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)  # в архиве с этого момента

    @property
    def purge_at(self) -> datetime | None:
        """Когда архивный проект удалится насовсем."""
        return self.archived_at + timedelta(days=ARCHIVE_DAYS) if self.archived_at else None


ARCHIVE_DAYS = 30  # столько проект лежит в архиве, потом удаляется вместе с данными


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ------------------------------------------------------------ подключения ---
# Что показывает экран «Подключения». key — поле Settings; secret — хранится зашифрованным
# и не возвращается в интерфейс; check — функция из check.py для кнопки «Проверить».
INTEGRATIONS: list[dict[str, Any]] = [
    {"id": "general", "title": "Проект", "fields": [
        ("business_name", "Название для отчётов", False), ("timezone", "Часовой пояс", False),
        ("currency", "Валюта (код)", False), ("currency_symbol", "Знак валюты", False),
        ("phone_region", "Страна телефонов (RU, AM…)", False)]},
    {"id": "direct", "title": "Яндекс Директ", "check": "direct", "fields": [
        ("direct_token", "OAuth-токен", True), ("direct_client_login", "Логин клиента (для агентских аккаунтов)", False),
        ("direct_mode", "Режим правок", False)]},
    {"id": "metrika", "title": "Яндекс Метрика", "check": "metrika", "picker": "metrika", "fields": [
        ("metrika_token", "OAuth-токен", True), ("metrika_counter_id", "Счётчики через запятую", False),
        ("metrika_goal_id", "Основная цель «заявка» (для офлайн-конверсий)", False),
        ("direct_goal_ids", "Цели-конверсии для Директа, через запятую", False)]},
    {"id": "topvisor", "title": "Топвизор", "check": "topvisor", "picker": "topvisor", "fields": [
        ("topvisor_user_id", "User ID", False), ("topvisor_api_key", "Ключ API", True),
        ("topvisor_project_id", "ID проектов через запятую", False), ("topvisor_region_index", "Индексы регионов (пусто — все)", False)]},
    {"id": "callibri", "title": "Callibri", "check": "callibri", "fields": [
        ("callibri_email", "Почта", False), ("callibri_token", "Токен API", True), ("callibri_site_id", "ID сайта", False)]},
    {"id": "bitrix", "title": "Битрикс24", "check": "bitrix", "fields": [
        ("bitrix_webhook_url", "Входящий вебхук", True), ("bitrix_chat_id", "Чат с заявками (chat123)", False),
        ("bitrix_party_date_field", "Поле «дата праздника» (UF_CRM_…)", False), ("bitrix_guests_field", "Поле «количество гостей»", False)]},
    {"id": "yclients", "title": "YCLIENTS (брони)", "check": "yclients", "fields": [
        ("yclients_partner_token", "Токен партнёра", True), ("yclients_user_token", "Токен пользователя", True),
        ("yclients_company_id", "ID филиала", False)]},
    {"id": "telegram", "title": "Telegram", "check": "telegram", "fields": [
        ("telegram_bot_token", "Токен бота", True), ("telegram_chat_id", "Рабочий чат для отчёта", False),
        ("telegram_leads_chat_id", "Чат с заявками", False)]},
    {"id": "sheets", "title": "Google-таблица заявок", "check": "sheets", "fields": [
        ("google_sheet_id", "ID таблицы", False), ("google_sheet_tab", "Лист", False)]},
    {"id": "ai", "title": "Нейросети", "fields": [
        ("direct_ai_provider", "По умолчанию: gemini, anthropic, openai, yandexgpt", False),
        ("gemini_api_key", "Ключ Gemini API", True), ("anthropic_api_key", "Ключ Claude API", True),
        ("openai_api_key", "Ключ OpenAI API", True), ("openai_base_url", "Адрес OpenAI-совместимого сервиса", False),
        ("yandexgpt_api_key", "Ключ YandexGPT", True), ("yandexgpt_folder_id", "Каталог YandexGPT", False)]},
]
FIELDS = {key: (label, secret) for block in INTEGRATIONS for key, label, secret in block["fields"]}
SECRET_FIELDS = {k for k, (_, s) in FIELDS.items() if s}


# ------------------------------------------------------------ шифрование ---
def _fernet() -> Fernet:
    from .config import ROOT

    raw = os.environ.get("PLATFORM_SECRET_KEY", "")
    if not raw:
        path = Path(os.environ.get("PLATFORM_SECRET_FILE") or ROOT / "secrets" / "platform.key")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pysecrets.token_urlsafe(32))
            path.chmod(0o600)
        raw = path.read_text().strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def _encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode() if data else ""


def _decrypt(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as e:
        raise RuntimeError("Не удалось расшифровать ключи проекта: изменился PLATFORM_SECRET_KEY") from e


# ------------------------------------------------------------ база платформы ---
_platform: tuple[str, Engine, sessionmaker] | None = None


def platform_url() -> str:
    from .config import base_settings

    s = base_settings()
    return s.platform_database_url or s.database_url


def _platform_session() -> Session:
    global _platform
    url = platform_url()
    if _platform is None or _platform[0] != url:
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        engine = create_engine(url, future=True, **kw)
        PlatformBase.metadata.create_all(engine)
        _add_missing_platform_columns(engine)
        _platform = (url, engine, sessionmaker(bind=engine, expire_on_commit=False))
    return _platform[2]()


def _add_missing_platform_columns(engine: Engine) -> None:
    """Новые nullable-столбцы в уже созданных таблицах платформы (как add_missing_columns в db.py)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in PlatformBase.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=engine.dialect)}"))


@contextmanager
def platform_session() -> Iterator[Session]:
    s = _platform_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_platform() -> None:
    global _platform
    _platform = None


# ------------------------------------------------------------ проекты ---
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                      "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(name: str) -> str:
    s = "".join(_TRANSLIT.get(c, c) for c in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:38]
    return s or "project"


def project_database_url(slug: str) -> str:
    from .config import base_settings

    base = base_settings().database_url
    url = make_url(base)
    if url.get_backend_name() == "sqlite":
        db = Path(url.database or "partyhub.db")
        return f"sqlite:///{db.parent / 'projects' / (slug + '.db')}"
    # PostgreSQL: отдельная схема на проект
    return url.update_query_dict({"options": f"-csearch_path=p_{slug.replace('-', '_')}"}).render_as_string(hide_password=False)


def _prepare_database(slug: str) -> None:
    url = make_url(project_database_url(slug))
    if url.get_backend_name() == "sqlite":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    else:
        engine = create_engine(url.difference_update_query(["options"]), future=True)
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "p_{slug.replace("-", "_")}"'))
        engine.dispose()


def _drop_database(slug: str) -> None:
    """Стирает данные проекта: SQLite-файл или схему p_<slug> в PostgreSQL."""
    from .db import forget_engine

    url = project_database_url(slug)
    forget_engine(url)
    u = make_url(url)
    if u.get_backend_name() == "sqlite":
        Path(u.database).unlink(missing_ok=True)
    else:
        engine = create_engine(u.difference_update_query(["options"]), future=True)
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "p_{slug.replace("-", "_")}" CASCADE'))
        engine.dispose()


def list_projects(archived: bool | None = False) -> list[Project]:
    """Активные проекты (по умолчанию), только архивные (archived=True) или все (None)."""
    q = select(Project).order_by(Project.name)
    if archived is not None:
        q = q.where(Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None))
    with platform_session() as s:
        return list(s.scalars(q))


def get_project(slug: str) -> Project | None:
    with platform_session() as s:
        return s.scalar(select(Project).where(Project.slug == slug))


def create_project(name: str, slug: str | None = None, values: dict | None = None) -> Project:
    name = name.strip()
    if not name:
        raise ValueError("Пустое название проекта")
    slug = (slug or slugify(name)).strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError("Адрес проекта: латиница, цифры и дефис, 2–39 символов")
    with platform_session() as s:
        if s.scalar(select(Project.id).where(Project.slug == slug)):
            raise ValueError(f"Проект с адресом «{slug}» уже есть")
        p = Project(slug=slug, name=name, settings_json=json.dumps(
            {"business_name": name, "webhook_token": pysecrets.token_urlsafe(18), "direct_mode": "dry_run"}))
        s.add(p)
    _prepare_database(slug)
    if values:
        update_settings(slug, values)
    return get_project(slug)


def archive_project(slug: str) -> Project:
    """В архив: проект пропадает из списка и планировщика, через ARCHIVE_DAYS дней удаляется."""
    with platform_session() as s:
        p = s.scalar(select(Project).where(Project.slug == slug).with_for_update())
        if not p:
            raise KeyError(slug)
        if p.archived_at is None:
            p.archived_at = _utcnow()
    return get_project(slug)


def restore_project(slug: str) -> Project:
    with platform_session() as s:
        p = s.scalar(select(Project).where(Project.slug == slug).with_for_update())
        if not p:
            raise KeyError(slug)
        p.archived_at = None
    return get_project(slug)


def purge_expired(now: datetime | None = None) -> list[str]:
    """Удаляет насовсем проекты, пролежавшие в архиве ARCHIVE_DAYS дней. Возвращает их адреса."""
    edge = (now or _utcnow()) - timedelta(days=ARCHIVE_DAYS)
    with platform_session() as s:
        slugs = list(s.scalars(select(Project.slug).where(Project.archived_at.is_not(None), Project.archived_at <= edge)))
    for slug in slugs:
        _drop_database(slug)
        with platform_session() as s:
            p = s.scalar(select(Project).where(Project.slug == slug))
            if p and p.archived_at is not None and p.archived_at <= edge:  # вдруг успели восстановить
                s.delete(p)
    return slugs


def update_settings(slug: str, values: dict[str, Any]) -> Project:
    """Сохраняет поля из INTEGRATIONS. Пустая строка в секретном поле — «не менять», None — стереть."""
    unknown = set(values) - set(FIELDS) - {"rules"}
    if unknown:
        raise ValueError(f"Неизвестные поля: {', '.join(sorted(unknown))}")
    if "direct_mode" in values and values["direct_mode"] not in ("dry_run", "sandbox", "live", "", None):
        raise ValueError("Режим Директа: dry_run, sandbox или live")
    if values.get("timezone"):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(str(values["timezone"]).strip())
        except Exception:
            raise ValueError(f"Неизвестный часовой пояс «{values['timezone']}»: нужен вид Europe/Moscow или Asia/Yerevan") from None
    if values.get("phone_region") and not re.fullmatch(r"[A-Za-z]{2}", str(values["phone_region"]).strip()):
        raise ValueError("Страна телефонов — две латинские буквы: RU, AM…")
    with platform_session() as s:
        p = s.scalar(select(Project).where(Project.slug == slug).with_for_update())
        if not p:
            raise KeyError(slug)
        plain, secret = json.loads(p.settings_json or "{}"), _decrypt(p.secrets_enc)
        for k, v in values.items():
            if k == "rules":
                p.rules_json = json.dumps(v or {}, ensure_ascii=False)
                continue
            target = secret if k in SECRET_FIELDS else plain
            if v is None or (v == "" and k not in SECRET_FIELDS):
                target.pop(k, None)
            elif v != "":
                target[k] = str(v).strip()
        p.settings_json = json.dumps(plain, ensure_ascii=False)
        p.secrets_enc = _encrypt(secret)
        p.version = (p.version or 0) + 1
    from .config import clear_caches

    clear_caches()
    return get_project(slug)


def overrides(p: Project) -> dict[str, Any]:
    """Поля Settings, которые проект задаёт поверх общих (.env)."""
    return {**json.loads(p.settings_json or "{}"), **_decrypt(p.secrets_enc), "database_url": project_database_url(p.slug)}


def project_rules(p: Project) -> dict:
    return json.loads(p.rules_json or "{}")


def _connected(fields: list[dict]) -> bool:
    """Подключено — задан ключ (у блоков без ключей — любое поле). Режим правок сам по себе не подключение."""
    keys = [f for f in fields if f["secret"]]
    return any(f["set"] for f in keys) if keys else any(f["set"] for f in fields)


def public_view(p: Project) -> dict:
    """Для интерфейса: обычные поля — значения, секретные — только «задан или нет»."""
    plain, secret = json.loads(p.settings_json or "{}"), _decrypt(p.secrets_enc)
    blocks = []
    for b in INTEGRATIONS:
        fields = [{"key": k, "label": label, "secret": sec, "value": "" if sec else plain.get(k, ""), "set": bool(secret.get(k) if sec else plain.get(k))}
                  for k, label, sec in b["fields"]]
        blocks.append({"id": b["id"], "title": b["title"], "check": b.get("check"), "picker": b.get("picker"), "fields": fields,
                       "connected": b["id"] != "general" and _connected(fields)})
    return {"slug": p.slug, "name": p.name, "webhook_token": plain.get("webhook_token", ""), "blocks": blocks,
            "rules": project_rules(p), "archived_at": _iso(p.archived_at), "purge_at": _iso(p.purge_at)}


def _iso(d: datetime | None) -> str | None:
    return d.replace(tzinfo=UTC).isoformat() if d else None


# ------------------------------------------------------------ контекст ---
def current_slug() -> str | None:
    return _current.get()


@contextmanager
def use_project(slug: str) -> Iterator[Project]:
    p = get_project(slug)
    if not p:
        raise KeyError(f"Нет проекта «{slug}»")
    token = _current.set(slug)
    try:
        from .db import create_all

        create_all()
        yield p
    finally:
        _current.reset(token)


def set_current(slug: str | None) -> contextvars.Token:
    return _current.set(slug)


def reset_current(token: contextvars.Token) -> None:
    _current.reset(token)
