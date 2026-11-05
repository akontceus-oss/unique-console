from datetime import datetime

from .config import get_settings

# Тесты подменяют это значение, чтобы «заморозить» время.
_frozen: datetime | None = None


def now() -> datetime:
    """Текущее время по часовому поясу площадки, без tzinfo (так оно и хранится в базе)."""
    if _frozen is not None:
        return _frozen
    return datetime.now(get_settings().tz).replace(tzinfo=None, microsecond=0)


def freeze(dt: datetime | None) -> None:
    global _frozen
    _frozen = dt
