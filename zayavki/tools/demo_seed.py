"""Заполняет пустую базу примерами, чтобы посмотреть панель. Не запускать на рабочей базе."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.auth import hash_password
from app.clock import now
from app.db import init_engine, session_scope
from app.main import create_lead, seed_sources
from app.models import Lead, Shift, Source, User

init_engine()
with session_scope() as s:
    if s.scalar(select(func.count(User.id))):
        raise SystemExit("База не пустая")
    seed_sources(s)
    s.flush()
    owner = User(login="owner", name="Андрей", role="owner", password_hash=hash_password("demo123"))
    admin = User(login="admin", name="Администратор", role="admin", password_hash=hash_password("demo123"))
    ms = [User(login=l, name=n, role="manager", color=c, password_hash=hash_password("demo123"))
          for l, n, c in [("liana", "Лиана", "#d6336c"), ("ekaterina", "Екатерина", "#1c7ed6"), ("egor", "Егор", "#2f9e44"), ("nikita", "Никита", "#f08c00")]]
    s.add_all([owner, admin, *ms])
    s.flush()
    t = now()
    for i in range(-3, 10):
        s.add(Shift(day=(t + timedelta(days=i)).date(), manager_id=ms[i % 4].id))
    s.flush()
    src = {x.name: x.id for x in s.scalars(select(Source))}
    samples = [
        (-2, 11, "+79506993082", "Елена", "Поиск Яндекса", "Реклама в яндексе", {"status": "бронь оплачена", "package": "Стандарт", "booking_date": (t + timedelta(days=2)).date().isoformat(), "prepaid_at": (t - timedelta(days=1)).date().isoformat(), "prepaid_amount": "5000"}),
        (-2, 13, "+79266741324", "Ирина", "Приходили раньше", "Заходила к нам, связаться в воскресенье с 12 до 16", {"status": "думает", "next_contact_at": (t - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")}),
        (-1, 10, "89857002777", "Милена", "Живут рядом, увидели вывеску", "Хочет узнать про День Рождения. Связь в MAX", {"status": "информация отправлена", "contact_channel": "МАКС"}),
        (-1, 15, "+79160921910", "Яна", "Группа ВКонтакте", "Интересует проведение мероприятия", {"status": "отказ", "refusal_reason": "дорого"}),
        (-1, 17, "+79802185504", "Сергей", "Реклама, не помнит где", "Интересует проведение мероприятия 12 ноября (11 лет)", {"status": "обсуждаем", "next_contact_at": (t + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")}),
        (0, -2, "+79939209539", "Анна", "Яндекс Карты", "Интересует проведение мероприятия 15 ноября. Искали на Яндекс картах картинг", {}),
        (0, -1, "+7 (985) 280-10-92", "Екатерина", "Интернет, не уточнили", "Хочет отпраздновать день рождения", {}),
    ]
    from app import clock
    for day, hour, phone, name, source, comment, upd in samples:
        at = (t + timedelta(days=day)).replace(hour=hour % 24, minute=10) if hour >= 0 else t + timedelta(minutes=hour * 20)
        clock.freeze(at)
        lead, _ = create_lead(s, {"phone": phone, "name": name, "source_id": src[source], "admin_comment": comment}, admin, "manual", at)
        for k, v in upd.items():
            setattr(lead, k, v if k not in ("booking_date", "prepaid_at") else datetime.fromisoformat(v).date())
        if "next_contact_at" in upd:
            lead.next_contact_at = datetime.strptime(upd["next_contact_at"], "%Y-%m-%d %H:%M")
        if "prepaid_amount" in upd:
            lead.prepaid_amount = int(upd["prepaid_amount"])
        if upd:
            lead.taken_at = at + timedelta(minutes=5)
            lead.touched_at = at + timedelta(minutes=30)
    clock.freeze(None)
print("Готово. Вход: owner / demo123, менеджеры liana, ekaterina, egor, nikita / demo123")
