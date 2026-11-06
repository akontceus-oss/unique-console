from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .clock import now
from .db import Base

ROLES = ("owner", "admin", "manager")

# Статусы взяты из выпадающего списка таблицы «Брони»; «новая» добавлена как начальный.
STATUSES = [
    "новая",
    "не дозвонились",
    "обсуждаем",
    "информация отправлена",
    "думает",
    "назначена экскурсия",
    "бронь оплачена",
    "просто покататься",
    "отказ",
    "Далеко",
    "нецелевой лид",
    "другое",
    "Перестал отвечать",
]
# Заявка в работе: по ней нужен следующий шаг.
OPEN_STATUSES = {"не дозвонились", "обсуждаем", "информация отправлена", "думает", "Перестал отвечать", "назначена экскурсия"}
CLOSED_STATUSES = {"отказ", "Далеко", "нецелевой лид", "просто покататься", "другое"}
REFUSAL_REASONS = ["дорого", "далеко", "не подошло время", "выбрали другой формат", "другое"]
PACKAGES = ["Мини", "Стандарт", "Медиум", "Премиум"]
CONTACT_CHANNELS = ["МАКС", "Telegram", "WhatsApp", "звонок", "СМС"]

# Источники «Как узнали» из разбора чата Битрикс24. По ним консоль (pult.drozdgid.ru) считает
# заявки и брони по источникам, когда у заявки нет UTM-метки и звонка через колтрекинг.
DEFAULT_SOURCES = [
    ("Реклама", "Яндекс Директ"),
    ("Реклама", "Реклама ВКонтакте"),
    ("Реклама", "Биглион"),
    ("Реклама", "Реклама на Ozon"),
    ("Реклама", "Реклама, не помнит где"),
    ("Поиск и карты", "Поиск Яндекса"),
    ("Поиск и карты", "Яндекс Карты"),
    ("Поиск и карты", "2ГИС"),
    ("Поиск и карты", "Интернет, не уточнили"),
    ("Сайт и соцсети", "Сайт"),
    ("Сайт и соцсети", "Instagram"),
    ("Сайт и соцсети", "Группа ВКонтакте"),
    ("Знакомые", "Друзья, подруга"),
    ("Знакомые", "Были на чужом празднике"),
    ("Уже знают нас", "Приходили раньше"),
    ("Уже знают нас", "Живут рядом, увидели вывеску"),
    ("Другое", "Другое"),
]


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="manager")
    password_hash: Mapped[str] = mapped_column(String(256))
    phone: Mapped[str] = mapped_column(String(32), default="")
    telegram: Mapped[str] = mapped_column(String(64), default="")
    # Цвет менеджера в расписании.
    color: Mapped[str] = mapped_column(String(16), default="#4f7cff")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Shift(Base):
    """Смена на день: заявки с 22:00 предыдущего дня до 21:59 этого дня получает этот менеджер.

    Поля start/end остались от прежней версии и в распределении не участвуют."""

    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("day", "manager_id", name="uq_shift_day_manager"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    start: Mapped[str] = mapped_column(String(5), default="08:00")
    end: Mapped[str] = mapped_column(String(5), default="22:00")
    manager: Mapped[User] = relationship()


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    group: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    order: Mapped[int] = mapped_column(Integer, default=0)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    # № за день у менеджера, как «№ п/п» в таблице
    day_no: Mapped[int] = mapped_column(Integer, default=0)
    # откуда пришла: manual / tilda-landing / tilda-site / site / ...
    channel: Mapped[str] = mapped_column(String(32), default="manual")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    phone: Mapped[str] = mapped_column(String(32), default="")
    phone_norm: Mapped[str] = mapped_column(String(16), default="", index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_text: Mapped[str] = mapped_column(Text, default="")
    utm_source: Mapped[str] = mapped_column(String(128), default="")
    utm_medium: Mapped[str] = mapped_column(String(128), default="")
    utm_campaign: Mapped[str] = mapped_column(String(256), default="")
    utm_content: Mapped[str] = mapped_column(String(256), default="")
    utm_term: Mapped[str] = mapped_column(String(256), default="")
    holiday_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    holiday_text: Mapped[str] = mapped_column(String(128), default="")
    admin_comment: Mapped[str] = mapped_column(Text, default="")

    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # пришла вне смены, менеджер увидит её на следующей смене
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="новая")
    refusal_reason: Mapped[str] = mapped_column(String(64), default="")
    contact_channel: Mapped[str] = mapped_column(String(32), default="")
    child_age: Mapped[str] = mapped_column(String(16), default="")
    kids: Mapped[str] = mapped_column(String(16), default="")
    adults: Mapped[str] = mapped_column(String(16), default="")
    package: Mapped[str] = mapped_column(String(64), default="")
    next_contact_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    manager_comment: Mapped[str] = mapped_column(Text, default="")
    review: Mapped[str] = mapped_column(Text, default="")

    # Бронь (как сообщение «Бронь: …» в чате)
    booking_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    booking_time: Mapped[str] = mapped_column(String(32), default="")
    booking_room: Mapped[str] = mapped_column(String(128), default="")
    booking_show: Mapped[str] = mapped_column(String(128), default="")
    prepaid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    prepaid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # когда заявку последний раз меняли (не считая автоматических полей)
    touched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    manager: Mapped[User | None] = relationship(foreign_keys=[manager_id])
    source: Mapped[Source | None] = relationship()


class LeadEvent(Base):
    """История изменений заявки."""

    __tablename__ = "lead_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field: Mapped[str] = mapped_column(String(64))
    old: Mapped[str] = mapped_column(Text, default="")
    new: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[User | None] = relationship()
