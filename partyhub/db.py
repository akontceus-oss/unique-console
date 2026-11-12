"""Подключение к базе данных. В продакшене — PostgreSQL, для демо и тестов — SQLite.

У каждого проекта своя база (см. projects.py): движок выбирается по DATABASE_URL текущих настроек.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engines: dict[str, tuple[Engine, sessionmaker]] = {}
_ready: set[str] = set()  # базы, где уже созданы таблицы
_lock = threading.Lock()
_default_url: str | None = None  # init_engine(url) вне проекта — как раньше, одна база


def _current_url() -> str:
    from .projects import current_slug

    if current_slug() is None and _default_url:
        return _default_url
    return get_settings().database_url


def _entry(url: str) -> tuple[Engine, sessionmaker]:
    with _lock:
        if url not in _engines:
            kwargs = {"future": True}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
            engine = create_engine(url, **kwargs)
            _engines[url] = (engine, sessionmaker(bind=engine, expire_on_commit=False))
        return _engines[url]


def init_engine(url: str | None = None) -> Engine:
    global _default_url
    from .projects import current_slug

    if current_slug() is None:
        _default_url = url or get_settings().database_url
        url = _default_url
    else:
        url = url or get_settings().database_url
    old = _engines.pop(url, None)
    if old:
        old[0].dispose()
    _ready.discard(url)
    return _entry(url)[0]


def forget_engine(url: str) -> None:
    """Закрывает подключения к базе (перед её удалением)."""
    with _lock:
        old = _engines.pop(url, None)
        _ready.discard(url)
    if old:
        old[0].dispose()


def get_engine() -> Engine:
    return _entry(_current_url())[0]


def create_all() -> None:
    from . import models  # noqa: F401  регистрирует таблицы

    url = _current_url()
    if url in _ready:
        return
    Base.metadata.create_all(get_engine())
    add_missing_columns()
    _ready.add(url)


def add_missing_columns() -> None:
    """Добавляет в существующие таблицы новые столбцы, которых там ещё нет.

    create_all создаёт недостающие таблицы, но не меняет уже созданные. Nullable-столбцы добавляются
    как есть, NOT NULL — с их значением по умолчанию (иначе старые строки не заполнить).
    """
    engine = get_engine()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                if not col.nullable:
                    # NOT NULL можно добавить только с постоянным значением по умолчанию — им заполнятся старые строки
                    lit = _default_literal(col)
                    if lit is None:
                        continue
                    ddl += f" NOT NULL DEFAULT {lit}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))


def _default_literal(col) -> str | None:
    """SQL-литерал для простого default у столбца (строка, число, bool) или None."""
    d = col.server_default.arg if col.server_default is not None and isinstance(col.server_default.arg, str) else None
    if d is not None:
        return "'" + d.replace("'", "''") + "'"
    if col.default is None or not col.default.is_scalar:
        return None
    v = col.default.arg
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _entry(_current_url())[1]()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
