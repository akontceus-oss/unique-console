import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret: str
    tz: ZoneInfo
    # токен для приёма заявок с сайтов (Тильда, самописный сайт, консоль)
    intake_token: str
    # токен, по которому консоль забирает заявки, статусы и оплаты
    export_token: str
    # сколько минут заявка считается «свежей» до яркой подсветки
    new_lead_alert_minutes: int = 15
    # повторный клиент уходит прежнему менеджеру, если прошло меньше N дней
    repeat_client_days: int = 30
    # смена менеджера на день D: заявки с 22:00 дня D-1 до 21:59 дня D
    shift_change_hour: int = 22
    cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("ZAYAVKI_DATABASE_URL", "sqlite:///./zayavki.db"),
        secret=os.environ.get("ZAYAVKI_SECRET", "dev-secret-change-me"),
        tz=ZoneInfo(os.environ.get("ZAYAVKI_TZ", "Europe/Moscow")),
        intake_token=os.environ.get("ZAYAVKI_INTAKE_TOKEN", ""),
        export_token=os.environ.get("ZAYAVKI_EXPORT_TOKEN", ""),
        cookie_secure=os.environ.get("ZAYAVKI_COOKIE_SECURE", "0") == "1",
    )
