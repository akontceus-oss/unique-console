from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .clock import now
from .db import Base

ROLES = ("owner", "admin", "manager")

# Статусы взяты из выпадающего списка таблицы «Брони»; «новая» добавлена как начальный.
STATUSES = [
    "новая",
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
OPEN_STATUSES = {"обсуждаем", "информация отправлена", "думает", "Перестал отвечать", "назначена экскурсия"}
# «Не дозвонились» убран 27.11: то же, что «не отвечает» (Перестал отвечать). Старые заявки переводятся сами.
OLD_STATUS_RENAMES = {"не дозвонились": "Перестал отвечать"}
# Удалённая заявка: не показывается в таблице, журнале и сводке, возвращается из фильтра «Удалённые»
DELETED_STATUS = "удалена"
CLOSED_STATUSES = {"отказ", "Далеко", "нецелевой лид", "просто покататься", "другое", DELETED_STATUS}
# «Жёлтые» статусы: с клиентом ещё работаем. Заявка переносится на следующую смену своего менеджера,
# не тронули до 14:00 — строка пульсирует. «Просто покататься» сюда не входит.
WAITING_STATUSES = {"информация отправлена", "обсуждаем", "думает", "назначена экскурсия", "Перестал отвечать"}
STALE_HOUR = 14
# Статистика менеджеров. Неквалифицированные: разговора не было или клиенту не нужен праздник.
UNQUALIFIED_STATUSES = {"новая", "Перестал отвечать", "не дозвонились", "просто покататься", "нецелевой лид", DELETED_STATUS}
BOOKED_STATUS = "бронь оплачена"
# Обработанная заявка: всё, что обязательно узнать на первом созвоне
PROCESSED_FIELDS = ("name", "phone", "status", "source_id", "child_age")
# Качество карточки: что должно быть заполнено; поля брони — только у забронированных
QUALITY_FIELDS = ("status", "source_id", "holiday_date", "child_age", "kids", "adults", "package", "manager_comment")
QUALITY_BOOKING_FIELDS = ("booking_date", "booking_room", "booking_time", "booking_show")
REFUSAL_REASONS = ["дорого", "далеко", "не подошло время", "выбрали другое место", "другое"]
PACKAGES = ["Мини", "Стандарт", "Медиум", "Премиум"]
CONTACT_CHANNELS = ["МАКС", "Telegram", "WhatsApp", "звонок", "СМС"]
# С какого сайта пришла заявка: метка site в адресе приёма → название в таблице
SITE_LABELS = {
    "site": "Сайт Happy Birthday MSK",
    "hbmsk": "Сайт Happy Birthday MSK",
    "tilda-vnukovo": "Сайт Внуково Crazy Drift",
    "tilda-hbmsk": "Сайт Happy Birthday MSK (Тильда)",
    "manual": "Вручную",
}
# Адрес сайта — ссылкой под названием
SITE_URLS = {"site": "happy-birthday-msk.ru", "hbmsk": "happy-birthday-msk.ru", "tilda-hbmsk": "happy-birthday-msk.ru",
             "tilda-vnukovo": "vnukovo-crazydrift.ru"}
# Вкладки карточки заявки. Значение вне списка можно вписать вручную («Другое»).
# Сетка выходного: три «цепочки» с шагом 4 часа (праздник 3 ч + час перерыва), см. app/booking.py
BOOKING_TIMES = [("Утро", ["10:00", "10:30", "11:00"]), ("Обед", ["14:00", "14:30", "15:00"]),
                 ("Вечер", ["18:00", "18:30", "19:00"])]
ROOMS = ["Монако", "Сингапур", "Барселона"]
# шоу-программы как на сайте happy-birthday-msk.ru (src/data/site.ts). «ЧВК» — то же, что «Что в коробке?»
SHOWS = ["Что в коробке?", "Шариковая дискотека", "Усатая вечеринка", "Игровой бум", "Мафия", "Тесла-шоу"]
PREPAID_PRESETS = [5000, 10000]

# Источники «Как узнали» из разбора чата Битрикс24. По ним консоль (pult.drozdgid.ru) считает
# заявки и брони по источникам, когда у заявки нет UTM-метки и звонка через колтрекинг.
DEFAULT_SOURCES = [
    # порядок — как в списке у менеджера (решение Андрея 25.11): реклама и интернет, потом рекомендации
    ("Реклама и интернет", "Яндекс Директ"),
    ("Реклама и интернет", "Реклама ВКонтакте"),
    ("Реклама и интернет", "Поиск Яндекса"),
    ("Реклама и интернет", "Поиск Google"),
    ("Реклама и интернет", "Яндекс Карты"),
    ("Реклама и интернет", "2ГИС"),
    ("Реклама и интернет", "Instagram"),
    ("Реклама и интернет", "Реклама на Ozon"),
    ("Реклама и интернет", "Биглион"),
    ("Реклама и интернет", "Комсомольская правда"),
    ("Реклама и интернет", "Time Out"),
    ("Реклама и интернет", "Интернет, не уточнили"),
    ("Реклама и интернет", "Сайт"),
    ("Рекомендации и знакомые", "Друзья, подруга"),
    ("Рекомендации и знакомые", "Были на чужом празднике"),
    ("Рекомендации и знакомые", "Приходили раньше"),
    ("Рекомендации и знакомые", "Живут рядом, увидели вывеску"),
]
# Убраны из списка 25.11: у старых заявок остаются, в выборе не показываются
HIDDEN_SOURCES = ["Реклама, не помнит где", "Группа ВКонтакте", "Другое"]
SOURCE_RENAMES = {"Тайм Аут": "Time Out"}


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
    # что ушло в Telegram о брони (JSON); пусто — не отправляли
    booking_sent: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    booking_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # id этого сообщения в чате

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

    # Пометки через запятую: «экскурсия» — клиент просил экскурсию (назначают отдельно)
    tags: Mapped[str] = mapped_column(String(128), default="")
    # Повторные заявки клиента за 24 часа вливаются сюда: сколько влилось и когда последняя
    merged_count: Mapped[int] = mapped_column(Integer, default=0)
    bumped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Клиент уже обращался раньше (больше суток назад): прошлая заявка
    prev_lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
