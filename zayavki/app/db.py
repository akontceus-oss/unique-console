from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def init_engine(url: str | None = None):
    global _engine, _Session
    url = url or get_settings().database_url
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    _engine = create_engine(url, **kw)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    from . import models  # noqa: F401  регистрирует таблицы
    Base.metadata.create_all(_engine)
    add_missing_columns(_engine)
    return _engine


def add_missing_columns(engine) -> list[str]:
    """create_all не меняет существующие таблицы. Новые колонки моделей дописываем сами
    (ALTER TABLE … ADD COLUMN), чтобы обновление панели не требовало ручной правки базы.
    Только добавление: удаление и переименование колонок — вручную."""
    insp, added = inspect(engine), []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
                dv = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(dv, bool):
                    ddl += " DEFAULT " + ("true" if dv else "false") if engine.dialect.name == "postgresql" else f" DEFAULT {int(dv)}"
                elif isinstance(dv, (int, float)):
                    ddl += f" DEFAULT {dv}"
                elif isinstance(dv, str):
                    ddl += " DEFAULT '" + dv.replace("'", "''") + "'"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added


def get_session() -> Session:
    if _Session is None:
        init_engine()
    return _Session()


@contextmanager
def session_scope():
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
