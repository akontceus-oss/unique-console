from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import clock, config
from app.phones import format_phone, normalize_phone


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAYAVKI_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("ZAYAVKI_INTAKE_TOKEN", "in-tok")
    monkeypatch.setenv("ZAYAVKI_EXPORT_TOKEN", "out-tok")
    config.get_settings.cache_clear()
    from app.auth import hash_password
    from app.db import session_scope
    from app.main import create_app
    from app.models import User

    app = create_app()
    with session_scope() as s:
        s.add(User(login="owner", name="Андрей", role="owner", password_hash=hash_password("secret1")))
    clock.freeze(datetime(2026, 11, 5, 12, 0))
    yield app
    clock.freeze(None)
    config.get_settings.cache_clear()


def login(app, who="owner", pw="secret1"):
    c = TestClient(app)
    r = c.post("/api/login", json={"login": who, "password": pw})
    assert r.status_code == 200, r.text
    return c


def make_manager(c, login_, name):
    r = c.post("/api/users", json={"login": login_, "name": name, "role": "manager", "password": "pass123"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_phone_normalize():
    for raw in ["+79506993082", "89506993082", "+7 (950) 699-30-82", "+ 79506993082", "79506993082.0", "9506993082"]:
        assert normalize_phone(raw) == "79506993082", raw
    assert normalize_phone("МАХ") == ""
    assert format_phone("79852801092") == "+7 985 280-10-92"


def test_login_required(env):
    c = TestClient(env)
    assert c.get("/api/leads").status_code == 401
    assert c.post("/api/login", json={"login": "owner", "password": "bad"}).status_code == 401


def test_lead_goes_to_manager_on_shift(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    egor = make_manager(c, "egor", "Егор")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": liana, "start": "08:00", "end": "22:00"})
    c.put("/api/shifts", json={"day": "2026-11-06", "manager_id": egor})

    r = c.post("/api/leads", json={"phone": "+7 (985) 280-10-92", "name": "Екатерина", "admin_comment": "Хочет ДР"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignment"]["reason"] == "shift"
    assert body["lead"]["manager"] == "Лиана"
    assert body["lead"]["phone"] == "+7 985 280-10-92"
    assert body["lead"]["day_no"] == 1
    assert body["lead"]["flag"] == "new"

    # сутки смены 6.11 начинаются 5.11 в 22:00: в 21:59 ещё Лиана, с 22:00 уже Егор
    clock.freeze(datetime(2026, 11, 5, 21, 59))
    assert c.post("/api/leads", json={"phone": "89161112230"}).json()["lead"]["manager"] == "Лиана"
    clock.freeze(datetime(2026, 11, 5, 22, 0))
    r = c.post("/api/leads", json={"phone": "89161112233", "name": "Яна"}).json()
    assert r["lead"]["manager"] == "Егор" and r["lead"]["late"] is False
    assert r["assignment"]["reason"] == "shift"


def test_no_shift_goes_to_next_and_bulk_days(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    # натыкали Лиане 5, 6 и 9 ноября одним запросом
    r = c.post("/api/shifts/bulk", json={"manager_id": liana, "days": ["2026-11-05", "2026-11-06", "2026-11-09"]})
    assert r.json()["changed"] == 3
    assert [x["day"] for x in c.get("/api/shifts", params={"month": "2026-11"}).json()] == ["2026-11-05", "2026-11-06", "2026-11-09"]
    # снять 6-е
    assert c.post("/api/shifts/bulk", json={"manager_id": liana, "days": ["2026-11-06"], "on": False}).json()["changed"] == 1
    # 6-го смены нет: заявка 5.11 в 22:30 уходит на смену 9-го с пометкой «поздняя»
    clock.freeze(datetime(2026, 11, 5, 22, 30))
    lead = c.post("/api/leads", json={"phone": "+79260000009"}).json()["lead"]
    assert lead["manager"] == "Лиана" and lead["late"] is True
    assert c.post("/api/shifts/bulk", json={"manager_id": liana, "days": []}).status_code == 422


def test_repeat_client_goes_to_same_manager(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    egor = make_manager(c, "egor", "Егор")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": liana})
    c.post("/api/leads", json={"phone": "+79991234567", "name": "Анна"})
    c.put("/api/shifts", json={"day": "2026-11-06", "manager_id": egor})
    clock.freeze(datetime(2026, 11, 6, 12, 0))
    chk = c.get("/api/leads/check-phone", params={"phone": "8 999 123-45-67"}).json()
    assert chk["duplicates"][0]["manager"] == "Лиана"
    assert chk["suggested"]["reason"] == "repeat"
    r = c.post("/api/leads", json={"phone": "8 999 123-45-67", "name": "Анна"}).json()
    assert r["lead"]["manager"] == "Лиана"


def test_two_on_shift_balance(env):
    c = login(env)
    a = make_manager(c, "a", "А")
    b = make_manager(c, "b", "Б")
    for m in (a, b):
        c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": m})
    got = [c.post("/api/leads", json={"phone": f"+7916000000{i}"}).json()["lead"]["manager"] for i in range(4)]
    assert sorted(got) == ["А", "А", "Б", "Б"]


def test_manager_sees_only_own_and_flags(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    make_manager(c, "egor", "Егор")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": liana})
    lead_id = c.post("/api/leads", json={"phone": "+79260000001"}).json()["lead"]["id"]

    egor = login(env, "egor", "pass123")
    assert egor.get("/api/leads").json()["total"] == 0
    assert egor.get(f"/api/leads/{lead_id}").status_code == 404

    m = login(env, "liana", "pass123")
    data = m.get("/api/leads").json()
    assert data["counters"]["new"] == 1

    # взял, поставил «думает» без следующего шага → жёлтая
    m.post(f"/api/leads/{lead_id}/take")
    r = m.patch(f"/api/leads/{lead_id}", json={"status": "думает"}).json()
    assert r["lead"]["flag"] == "nostep"
    # назначил контакт на сегодня 16:00 → оранжевая
    r = m.patch(f"/api/leads/{lead_id}", json={"next_contact_at": "2026-11-05T16:00"}).json()
    assert r["lead"]["flag"] == "today"
    # время прошло, заявку не трогали → красная
    clock.freeze(datetime(2026, 11, 5, 16, 30))
    assert m.get(f"/api/leads/{lead_id}").json()["lead"]["flag"] == "overdue"
    # уведомление о подошедшем контакте
    n = m.get("/api/notifications", params={"since": "2026-11-05T15:59"}).json()
    assert [x["id"] for x in n["due"]] == [lead_id]
    # написал комментарий → просрочка снята, снова нет следующего шага
    r = m.patch(f"/api/leads/{lead_id}", json={"manager_comment": "Не дозвонилась, написала в МАКС"}).json()
    assert r["lead"]["flag"] == "nostep"
    # менеджер не может отдать заявку другому
    assert m.patch(f"/api/leads/{lead_id}", json={"manager_id": 1}).status_code == 403


def test_booking_flags(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": liana})
    lid = c.post("/api/leads", json={"phone": "+79260000002"}).json()["lead"]["id"]
    r = c.patch(f"/api/leads/{lid}", json={"status": "бронь оплачена", "booking_date": "2026-11-07", "package": "Премиум",
                                           "prepaid_at": "2026-11-05", "prepaid_amount": "5 000"}).json()
    assert r["lead"]["flag"] == "soon" and r["lead"]["prepaid_amount"] == 5000
    clock.freeze(datetime(2026, 11, 8, 10, 0))
    assert c.get(f"/api/leads/{lid}").json()["lead"]["flag"] == "review"
    assert c.patch(f"/api/leads/{lid}", json={"review": "Всё понравилось"}).json()["lead"]["flag"] is None


def test_history_kept(env):
    c = login(env)
    lid = c.post("/api/leads", json={"phone": "+79260000003"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"status": "отказ", "refusal_reason": "дорого"})
    h = c.get(f"/api/leads/{lid}").json()["history"]
    assert [e["field"] for e in h][-2:] == ["status", "refusal_reason"]
    assert c.patch(f"/api/leads/{lid}", json={"status": "непонятно"}).status_code == 422


def test_intake_tilda_and_export(env):
    c = login(env)
    liana = make_manager(c, "liana", "Лиана")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": liana})
    anon = TestClient(env)
    assert anon.post("/api/intake?token=bad", data={"Phone": "+79260000004"}).status_code == 403
    assert anon.post("/api/intake?token=in-tok", data={"test": "test"}).json()["test"] is True
    r = anon.post("/api/intake?token=in-tok&site=tilda-landing",
                  data={"Name": "Вероника", "Phone": "+7 (963) 969-00-38", "utm_source": "yandex", "utm_campaign": "dr"})
    assert r.status_code == 200, r.text
    lead = c.get(f"/api/leads/{r.json()['id']}").json()["lead"]
    assert lead["manager"] == "Лиана" and lead["channel"] == "tilda-landing"
    assert lead["source"] == "Реклама и интернет · Яндекс Директ" and lead["utm"]["campaign"] == "dr"

    assert anon.get("/api/export/leads").status_code == 403
    exp = anon.get("/api/export/leads", headers={"x-export-token": "out-tok"}).json()
    assert exp["leads"][0]["utm"]["source"] == "yandex"



def test_intake_own_site_fields(env):
    """Свой сайт (happy-birthday-msk.ru) шлёт JSON: дата, возраст, дети, пакет и связь раскладываются по полям."""
    c = login(env)
    anon = TestClient(env)
    r = anon.post("/api/intake?site=site", headers={"x-intake-token": "in-tok"},
                  json={"phone": "+7 926 000-00-05", "name": "Ольга", "date": "2026-12-12", "age": "8", "kids": "10",
                        "pkg": "Медиум", "contact": "MAX", "comment": "Страница: /7-let", "source_name": "Яндекс Директ"})
    assert r.status_code == 200, r.text
    lead = c.get(f"/api/leads/{r.json()['id']}").json()["lead"]
    assert lead["holiday_date"] == "2026-12-12" and lead["child_age"] == "8" and lead["kids"] == "10"
    assert lead["package"] == "Медиум" and lead["contact_channel"] == "МАКС" and lead["channel"] == "site"
    assert lead["source"] == "Реклама и интернет · Яндекс Директ" and "/7-let" in lead["admin_comment"]
    # непонятная дата и пакет вне списка заявку не ломают
    r = anon.post("/api/intake?site=site", headers={"x-intake-token": "in-tok"},
                  json={"phone": "+79260000006", "date": "на выходных", "pkg": "Колесо подарков"})
    assert r.status_code == 200, r.text

def test_users_and_shift_copy(env):
    c = login(env)
    m = make_manager(c, "nikita", "Никита")
    c.put("/api/shifts", json={"day": "2026-11-02", "manager_id": m, "start": "10:00", "end": "20:00"})
    assert c.post("/api/shifts/copy-week", json={"src": "2026-11-02", "dst": "2026-11-09"}).json()["created"] == 1
    days = [x["day"] for x in c.get("/api/shifts", params={"month": "2026-11"}).json()]
    assert days == ["2026-11-02", "2026-11-09"]
    # уволенный менеджер не получает заявок
    c.patch(f"/api/users/{m}", json={"active": False})
    clock.freeze(datetime(2026, 11, 9, 12, 0))
    assert c.post("/api/leads", json={"phone": "+79260000005"}).json()["lead"]["manager_id"] is None
    mgr = login(env, "owner")
    assert mgr.get("/api/stats").status_code == 200


def test_new_sources_and_export_for_console(env):
    from app.db import session_scope
    from app.main import seed_sources
    from app.models import Source

    c = login(env)
    names = {x["name"]: x for x in c.get("/api/meta").json()["sources"]}
    assert {"Instagram", "2ГИС", "Реклама на Ozon", "Были на чужом празднике", "Яндекс Директ"} <= set(names)
    # в работающей базе новые каналы дописываются, а скрытые админом не возвращаются и не дублируются
    with session_scope() as s:
        for x in s.query(Source).filter(Source.name.in_(["2ГИС", "Реклама на Ozon"])):
            s.delete(x)
        s.query(Source).filter(Source.name == "Instagram").one().active = False
    with session_scope() as s:
        seed_sources(s)
    with session_scope() as s:
        assert s.query(Source).filter(Source.name == "2ГИС").count() == 1
        assert s.query(Source).filter(Source.name == "Instagram").one().active is False

    gis = c.get("/api/meta").json()["sources"]
    gis = next(x for x in gis if x["name"] == "2ГИС")
    r = c.post("/api/leads", json={"phone": "+79260000011", "source_id": gis["id"]})
    assert r.status_code == 200, r.text
    anon = TestClient(env)
    assert anon.post("/api/intake?token=in-tok", data={"Phone": "+79260000012", "utm_source": "instagram"}).status_code == 200
    h = {"x-export-token": "out-tok"}
    leads = anon.get("/api/export/leads", headers=h, params={"created_from": "2026-11-05", "created_to": "2026-11-05"}).json()["leads"]
    by_phone = {x["phone_norm"]: x for x in leads}
    assert by_phone["79260000011"]["source_group"] == "Реклама и интернет" and by_phone["79260000011"]["source_name"] == "2ГИС"
    assert by_phone["79260000012"]["source_name"] == "Instagram"
    assert anon.get("/api/export/leads", headers=h, params={"created_from": "2026-11-06"}).json()["leads"] == []


def test_several_holiday_dates(env):
    c = login(env)
    lead = c.post("/api/leads", json={"phone": "+79260000010", "holiday_dates": ["2026-11-15", "2026-11-14"]}).json()["lead"]
    assert lead["holiday_dates"] == ["2026-11-14", "2026-11-15"] and lead["holiday_date"] == "2026-11-14"
    r = c.patch(f"/api/leads/{lead['id']}", json={"holiday_dates": ["2026-11-21"]}).json()["lead"]
    assert r["holiday_dates"] == ["2026-11-21"] and r["holiday_text"] == ""
    assert c.patch(f"/api/leads/{lead['id']}", json={"holiday_dates": []}).json()["lead"]["holiday_dates"] == []
    assert c.patch(f"/api/leads/{lead['id']}", json={"holiday_dates": ["завтра"]}).status_code == 422


def test_journal_and_booking_check(env):
    """Журнал записи: брони комнат на день, точки в календаре, проверка правил, чужие брони у менеджера."""
    c = login(env)
    olga = make_manager(c, "olga", "Ольга")
    lead = lambda phone, **kw: c.post("/api/leads", json={"phone": phone, "manager_id": olga, **kw}).json()["lead"]["id"]  # noqa: E731
    a = lead("+79260000101", name="Ирина", booking_date="2026-11-28", booking_time="11:00", booking_room="Монако",
             package="Премиум", kids="9", adults="7", booking_show="Игровой бум", prepaid_amount="5000")
    lead("+79260000102", booking_date="2026-11-28", booking_time="14:30", booking_room="Сингапур")
    refused = lead("+79260000103", booking_date="2026-11-28", booking_time="18:00", booking_room="Барселона")
    c.patch(f"/api/leads/{refused}", json={"status": "отказ"})
    lead("+79260000104", booking_date="2026-11-28", booking_time="когда-нибудь", booking_room="Барселона")

    j = c.get("/api/journal?date=2026-11-28").json()
    assert j["weekend"] and [b["start"] for b in j["bookings"]] == ["11:00", "14:30"]   # без отказа и без непонятного времени
    mon = next(b for b in j["bookings"] if b["id"] == a)
    assert mon["end"] == "14:00" and mon["paid"] and mon["show"] == "Игровой бум" and mon["name"] == "Ирина"
    sl = {x["time"]: x for x in j["slots"]["Монако"]}
    assert sl["15:00"]["grid"] and sl["14:30"]["level"] == "red"
    assert c.get("/api/journal/month?month=2026-11").json()["days"] == {"2026-11-28": 2}

    w = c.get("/api/bookings/check", params={"date": "2026-11-28", "room": "Монако", "time": "14:30"}).json()["warnings"]
    assert any(x["level"] == "red" for x in w)
    # своя бронь не мешает сама себе при переносе
    assert c.get("/api/bookings/check", params={"date": "2026-11-28", "room": "Монако", "time": "11:00", "exclude_id": a}).json()["warnings"] == []

    # менеджер без своих броней видит чужие только как «занято»
    c.post("/api/users", json={"login": "lena", "name": "Лена", "role": "manager", "password": "pass123"})
    other = login(env, "lena", "pass123").get("/api/journal?date=2026-11-28").json()["bookings"]
    assert len(other) == 2 and not other[0]["own"] and "phone" not in other[0]


def test_delete_and_restore_booking(env):
    """Удалить бронь можно, она уходит в «Удалённые» и возвращается, если место свободно."""
    c = login(env)
    olga = make_manager(c, "olga2", "Ольга")
    new = lambda phone, t: c.post("/api/leads", json={"phone": phone, "manager_id": olga, "booking_date": "2026-11-28",  # noqa: E731
                                                     "booking_time": t, "booking_room": "Монако"}).json()["lead"]["id"]
    a = new("+79260000201", "11:00")
    r = c.post(f"/api/leads/{a}/booking/delete")
    assert r.status_code == 200 and r.json()["lead"]["booking_date"] is None
    assert c.get("/api/journal?date=2026-11-28").json()["bookings"] == []
    deleted = c.get("/api/bookings/deleted").json()
    assert len(deleted) == 1 and deleted[0]["booking_time"] == "11:00" and deleted[0]["booking_room"] == "Монако"
    assert c.post(f"/api/leads/{a}/booking/delete").status_code == 422          # брони уже нет

    # на это место поставили другой праздник — вернуть можно только принудительно
    b = new("+79260000202", "11:30")
    ev = deleted[0]["event_id"]
    r = c.post(f"/api/bookings/deleted/{ev}/restore")
    assert r.status_code == 409 and r.json()["conflict"]
    c.post(f"/api/leads/{b}/booking/delete")
    r = c.post(f"/api/bookings/deleted/{ev}/restore")
    assert r.status_code == 200 and r.json()["lead"]["booking_time"] == "11:00"
    assert [x["lead_id"] for x in c.get("/api/bookings/deleted").json()] == [b]   # вернувшаяся ушла из списка
    assert c.post(f"/api/bookings/deleted/{ev}/restore").status_code == 404
    # чужой менеджер не видит и не удаляет
    c.post("/api/users", json={"login": "lena2", "name": "Лена", "role": "manager", "password": "pass123"})
    lena = login(env, "lena2", "pass123")
    assert lena.get("/api/bookings/deleted").status_code == 403 and lena.post(f"/api/leads/{a}/booking/delete").status_code == 403
    # менеджер не удаляет брони, даже свои — только администратор и владелец
    assert login(env, "olga2", "pass123").post(f"/api/leads/{a}/booking/delete").status_code == 403


def test_waiting_leads_carry_to_managers_next_shift(env):
    """«Жёлтая» заявка остаётся у своего менеджера и поднимается в его следующую смену."""
    c = login(env)
    olga = make_manager(c, "olga3", "Ольга")
    clock.freeze(datetime(2026, 11, 24, 12, 0))
    lid = c.post("/api/leads", json={"phone": "+79260000301", "manager_id": olga}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"status": "думает"})
    clock.freeze(datetime(2026, 11, 26, 15, 0))   # через день, после 14:00
    rows = lambda: {r["id"]: r for r in c.get("/api/leads?date_from=2026-11-26&date_to=2026-11-26").json()["leads"]}  # noqa: E731
    assert lid not in rows()                         # Ольга не на смене — заявка не поднимается
    c.put("/api/shifts", json={"day": "2026-11-26", "manager_id": olga})
    r = rows()[lid]
    assert r["carry"] and r["stale"] and r["manager_id"] == olga   # на смене: поднялась и мигает, менеджер тот же
    c.patch(f"/api/leads/{lid}", json={"manager_comment": "перезвонила, думает до пятницы"})
    assert not rows()[lid]["stale"]                  # обработали сегодня — не мигает
    # «не отвечает» тоже переносится, «покататься» и отказ — нет
    for st, carried in (("Перестал отвечать", True), ("просто покататься", False), ("отказ", False), ("назначена экскурсия", True)):
        c.patch(f"/api/leads/{lid}", json={"status": st, "refusal_reason": "дорого" if st == "отказ" else ""})
        assert (lid in rows()) == carried, st



def test_sources_relayout_once_on_old_database(env):
    """Старая база: каналы встают в новый порядок, «Тайм Аут» → «Time Out», лишние скрыты — один раз."""
    from app.db import session_scope
    from app.main import seed_sources
    from app.models import Source
    from sqlalchemy import delete, select
    c = login(env)
    with session_scope() as s:   # имитируем базу до 25.11
        s.execute(delete(Source))
        for i, (g, n) in enumerate([("Реклама", "Яндекс Директ"), ("Реклама", "Тайм Аут"), ("Реклама", "Реклама, не помнит где"),
                                    ("Поиск и карты", "2ГИС"), ("Знакомые", "Друзья, подруга"), ("Свои", "Партнёр Х")]):
            s.add(Source(group=g, name=n, order=i))
    with session_scope() as s:
        seed_sources(s)
    src = c.get("/api/meta").json()["sources"]
    names = [x["name"] for x in src if x["active"]]
    assert names[:4] == ["Яндекс Директ", "Реклама ВКонтакте", "Поиск Яндекса", "Поиск Google"]
    assert "Time Out" in names and "Тайм Аут" not in names and "Реклама, не помнит где" not in names
    assert "Партнёр Х" in names                                   # свой канал администратора остался
    assert next(x for x in src if x["name"] == "Друзья, подруга")["group"] == "Рекомендации и знакомые"
    # повторный запуск порядок не трогает: администратор вернул скрытый канал — он остаётся
    with session_scope() as s:
        s.scalar(select(Source).where(Source.name == "Реклама, не помнит где")).active = True
    with session_scope() as s:
        seed_sources(s)
    assert any(x["name"] == "Реклама, не помнит где" and x["active"] for x in c.get("/api/meta").json()["sources"])


def test_delete_and_restore_lead(env):
    """Тестовую заявку удаляют (только администратор); она пропадает из таблицы и журнала, возвращается с прежним статусом."""
    c = login(env)
    olga = make_manager(c, "olga4", "Ольга")
    lid = c.post("/api/leads", json={"phone": "+79260000401", "manager_id": olga, "booking_date": "2026-11-28",
                                     "booking_time": "11:00", "booking_room": "Монако"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"status": "думает"})
    ids = lambda q="": [l["id"] for l in c.get("/api/leads" + q).json()["leads"]]  # noqa: E731
    c.post("/api/users", json={"login": "olga4m", "name": "Мен", "role": "manager", "password": "pass123"})
    assert login(env, "olga4m", "pass123").post(f"/api/leads/{lid}/delete").status_code == 403   # менеджер не может
    c.post("/api/users", json={"login": "adm4", "name": "Админ", "role": "admin", "password": "pass123"})
    adm = login(env, "adm4", "pass123")
    assert adm.post(f"/api/leads/{lid}/delete").status_code == 403                                # администратор тоже не может
    assert adm.post(f"/api/leads/{lid}/booking/delete").status_code == 200                        # а бронь удалить может
    adm.post(f"/api/bookings/deleted/{adm.get('/api/bookings/deleted').json()[0]['event_id']}/restore")
    assert c.post(f"/api/leads/{lid}/delete").status_code == 200
    assert lid not in ids() and lid in ids("?status=удалена")
    assert c.get("/api/journal?date=2026-11-28").json()["bookings"] == []
    assert c.get("/api/leads/check-phone?phone=%2B79260000401").json()["duplicates"] == []
    r = c.post(f"/api/leads/{lid}/restore")
    assert r.status_code == 200 and r.json()["lead"]["status"] == "думает" and lid in ids()


def test_repeat_requests_merge_within_24h_and_link_after(env):
    """Две заявки клиента за сутки — одна строка; через сутки — новая с пометкой о прошлой."""
    c = login(env)
    olga = make_manager(c, "olga5", "Ольга")
    c.put("/api/shifts", json={"day": "2026-11-05", "manager_id": olga})
    anon = TestClient(env)
    send = lambda **kw: anon.post("/api/intake?site=site", headers={"x-intake-token": "in-tok"}, json={"phone": "+79200100141", **kw}).json()  # noqa: E731
    clock.freeze(datetime(2026, 11, 5, 9, 45))
    first = send(comment="Мероприятие: Экскурсия по картингу\nПодарок: 10 минут катания", utm_source="yandex")
    lid = first["id"]
    c.patch(f"/api/leads/{lid}", json={"status": "обсуждаем"})          # менеджер успел поработать
    clock.freeze(datetime(2026, 11, 5, 9, 47))
    second = send(name="Ольга", date="2026-12-06", pkg="Стандарт", comment="Форма: Карточка тарифа")
    assert second["merged"] and second["id"] == lid
    lead = next(l for l in c.get("/api/leads").json()["leads"] if l["id"] == lid)
    assert lead["merged_count"] == 1 and lead["again"] and lead["status"] == "обсуждаем"
    assert lead["name"] == "Ольга" and lead["package"] == "Стандарт" and lead["holiday_date"] == "2026-12-06"
    assert "экскурсия" in lead["tags"] and "+ ещё заявка 05.11 09:47" in lead["admin_comment"]
    assert len([l for l in c.get("/api/leads").json()["leads"] if l["phone_norm"] == "79200100141"]) == 1
    assert [l["id"] for l in c.get(f"/api/notifications?since=2026-11-05T09:46:00").json()["again"]] == [lid]
    c.patch(f"/api/leads/{lid}", json={"manager_comment": "перезвонила"})
    assert not next(l for l in c.get("/api/leads").json()["leads"] if l["id"] == lid)["again"]   # отреагировали — не наверху
    # через сутки после последней заявки — новая строка, с пометкой о прошлой и подтянутыми данными
    clock.freeze(datetime(2026, 11, 6, 10, 0))
    third = send(comment="Хочу на 7 декабря")
    assert "merged" not in third and third["id"] != lid
    new = next(l for l in c.get("/api/leads").json()["leads"] if l["id"] == third["id"])
    assert new["prev"]["id"] == lid and new["prev"]["manager"] == "Ольга" and new["name"] == "Ольга" and new["package"] == "Стандарт"
    assert new["tags"] == []


def test_untaken_new_lead_triggers_alarm_after_8_minutes(env):
    c = login(env)
    olga = make_manager(c, "olga6", "Ольга")
    clock.freeze(datetime(2026, 11, 5, 12, 0))
    lid = c.post("/api/leads", json={"phone": "+79260000601", "manager_id": olga}).json()["lead"]["id"]
    m = login(env, "olga6", "pass123")
    clock.freeze(datetime(2026, 11, 5, 12, 7))
    assert m.get("/api/notifications?since=2026-11-05T12:06:30").json()["waiting"] == []        # 7 минут — ещё нет
    assert not [l for l in m.get("/api/leads").json()["leads"] if l["id"] == lid][0]["alert"]
    clock.freeze(datetime(2026, 11, 5, 12, 8))
    w = m.get("/api/notifications?since=2026-11-05T12:07:30").json()["waiting"]
    assert [x["id"] for x in w] == [lid] and w[0]["minutes"] == 8                                 # 8 минут — сирена
    assert [l for l in m.get("/api/leads").json()["leads"] if l["id"] == lid][0]["alert"]
    m.patch(f"/api/leads/{lid}", json={"manager_comment": "звоню"})                              # менеджер взял
    assert m.get("/api/notifications?since=2026-11-05T12:07:30").json()["waiting"] == []



def test_manual_repeat_merges_too(env):
    c = login(env)
    olga = make_manager(c, "olga7", "Ольга")
    a = c.post("/api/leads", json={"phone": "+79888888888", "manager_id": olga, "holiday_dates": ["2026-11-28"]}).json()
    assert c.get("/api/leads/check-phone?phone=%2B79888888888").json()["merge_into"]["id"] == a["lead"]["id"]
    b = c.post("/api/leads", json={"phone": "+7 988 888-88-88", "name": "Тест", "admin_comment": "Хочет экскурсию"}).json()
    assert b["merged"] and b["lead"]["id"] == a["lead"]["id"] and b["lead"]["name"] == "Тест" and "экскурсия" in b["lead"]["tags"]
    assert len([l for l in c.get("/api/leads").json()["leads"] if l["phone_norm"] == "79888888888"]) == 1


def test_index_busts_cache(env):
    r = TestClient(env).get("/")
    assert "/static/app.js?v=" in r.text and "/static/style.css?v=" in r.text and r.headers["cache-control"] == "no-cache"


def test_comment_columns_belong_to_roles(env):
    """Комментарий администратора — администратор и владелец; банкетного менеджера — менеджер и владелец."""
    c = login(env)
    olga = make_manager(c, "olga8", "Ольга")
    c.post("/api/users", json={"login": "adm8", "name": "Админ", "role": "admin", "password": "pass123"})
    lid = c.post("/api/leads", json={"phone": "+79260000801", "manager_id": olga}).json()["lead"]["id"]
    adm, man = login(env, "adm8", "pass123"), login(env, "olga8", "pass123")
    assert adm.patch(f"/api/leads/{lid}", json={"admin_comment": "звонила"}).status_code == 200
    assert adm.patch(f"/api/leads/{lid}", json={"manager_comment": "x"}).status_code == 403
    assert man.patch(f"/api/leads/{lid}", json={"manager_comment": "перезвоню"}).status_code == 200
    assert man.patch(f"/api/leads/{lid}", json={"admin_comment": "x"}).status_code == 403
    assert man.patch(f"/api/leads/{lid}", json={"review": "всё понравилось"}).status_code == 200
    assert adm.patch(f"/api/leads/{lid}", json={"review": "отзыв на Картах"}).status_code == 200
    assert c.patch(f"/api/leads/{lid}", json={"admin_comment": "в", "manager_comment": "л"}).status_code == 200


def test_manager_stats_month(env):
    """Итоги менеджера: квалифицированные без «покататься» и «новых», конверсия, скорость обработки, качество."""
    c = login(env)
    olga = make_manager(c, "olga9", "Ольга")
    src = c.get("/api/meta").json()["sources"][0]["id"]
    clock.freeze(datetime(2026, 11, 10, 12, 0))
    mk = lambda ph: c.post("/api/leads", json={"phone": ph, "manager_id": olga}).json()["lead"]["id"]  # noqa: E731
    a, b, d, e = mk("+79260000901"), mk("+79260000902"), mk("+79260000903"), mk("+79260000904")
    m = login(env, "olga9", "pass123")
    clock.freeze(datetime(2026, 11, 10, 12, 10))
    m.patch(f"/api/leads/{a}", json={"name": "Аня", "source_id": src, "child_age": "7", "status": "бронь оплачена", "prepaid_amount": 5000,
                                     "holiday_date": "2026-11-20", "kids": "8", "adults": "6", "package": "Стандарт", "manager_comment": "ок",
                                     "booking_date": "2026-11-20", "booking_room": "Монако", "booking_time": "11:00", "booking_show": "Мафия"})
    clock.freeze(datetime(2026, 11, 10, 13, 0))
    m.patch(f"/api/leads/{b}", json={"name": "Боря", "source_id": src, "child_age": "9", "status": "думает"})
    m.patch(f"/api/leads/{d}", json={"status": "просто покататься"})
    c.post("/api/users", json={"login": "adm9", "name": "Админ", "role": "admin", "password": "pass123"})
    assert login(env, "adm9", "pass123").get("/api/manager-stats?month=2026-11").status_code == 403   # только владелец
    clock.freeze(datetime(2026, 11, 25, 12, 0))
    r = c.get("/api/manager-stats?month=2026-11").json()["cur"]
    o = next(x for x in r["managers"] if x["name"] == "Ольга")
    assert (o["leads"], o["qualified"], o["booked"], o["conversion"]) == (4, 2, 1, 50)
    assert o["processed"] == 2 and o["speed_median_min"] == 35 and o["fast_share"] == 100           # 10 и 60 минут
    assert o["holidays_past"] == 1 and o["review_share"] == 0
    assert 0 < o["quality"] < 100 and "участников" in o["often_missing"]
    m.patch(f"/api/leads/{a}", json={"review": "супер"})
    assert next(x for x in c.get("/api/manager-stats?month=2026-11").json()["cur"]["managers"] if x["name"] == "Ольга")["review_share"] == 100


def test_booking_telegram_one_live_message(env, monkeypatch):
    """Бронь — в чат; изменили — прежнее удаляется и приходит «изменена»; сняли — прежнее зачёркнуто."""
    from app import tg_booking
    sent = []
    monkeypatch.setenv("ZAYAVKI_TG_BOT_TOKEN", "t")
    monkeypatch.setenv("ZAYAVKI_TG_BOOKING_CHAT", "-1")
    monkeypatch.setattr(tg_booking, "send", lambda lead_id, text, old_id=None, old_text=None, cancel=False: sent.append((text, old_id, cancel)))
    c = login(env)
    olga = make_manager(c, "olga10", "Лиана")
    lid = c.post("/api/leads", json={"phone": "+79629311665", "name": "Константин", "manager_id": olga}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"status": "бронь оплачена", "prepaid_amount": 5000, "package": "Премиум"})
    assert sent == []                                                              # даты и комнаты ещё нет
    c.patch(f"/api/leads/{lid}", json={"booking_date": "2026-12-05", "booking_time": "14:30", "booking_room": "Барселона"})
    text = sent[-1][0]
    assert "🎉 Бронь" in text and "Менеджер: Лиана" in text and "Дата: 5 декабря" in text and "Время: 14:30–17:30" in text
    assert "Шоу: выбирают" in text and "Оплачено ✅ 5 000 ₽" in text and "Заказчик: Константин" in text and "Номер: +7 962 931-16-65" in text
    from app.db import session_scope
    from app.models import Lead
    with session_scope() as s:                                                     # будто Telegram вернул id сообщения
        s.get(Lead, lid).booking_msg_id = 77
    c.patch(f"/api/leads/{lid}", json={"admin_comment": "просто комментарий", "name": "Костя"})
    assert len(sent) == 1                                                          # не бронь — молчим
    c.patch(f"/api/leads/{lid}", json={"booking_time": "16:00"})
    text, old_id, cancel = sent[-1]
    assert old_id == 77 and not cancel and "✏️ Бронь изменена" in text and "(было: 14:30–17:30)" in text
    c.post(f"/api/leads/{lid}/booking/delete")
    assert sent[-1][0] is None and sent[-1][2] is True                             # отмена: старое зачёркнуто


def test_booking_telegram_post_deletes_or_strikes(env, monkeypatch):
    from app import tg_booking
    from app.db import session_scope
    from app.models import Lead
    calls = []

    def api(method, **p):
        calls.append((method, p))
        if method == "deleteMessage":
            raise RuntimeError("старше 48 часов")
        return {"message_id": 501} if method == "sendMessage" else True
    monkeypatch.setattr(tg_booking, "_api", api)
    c = login(env)
    lid = c.post("/api/leads", json={"phone": "+79260001001"}).json()["lead"]["id"]
    tg_booking._post(lid, "новая", 77, "старая", cancel=False)
    assert [m for m, _ in calls] == ["deleteMessage", "editMessageText", "sendMessage"] and "<s>старая</s>" in calls[1][1]["text"]
    with session_scope() as s:
        assert s.get(Lead, lid).booking_msg_id == 501



def test_booking_telegram_mixed_package_note():
    from app.models import mixed_package
    assert mixed_package("Медиум + Премиум") and mixed_package("Стандарт ×2 + Медиум")
    assert not mixed_package("Медиум ×2") and not mixed_package("Премиум") and not mixed_package("")
    from datetime import date
    from app import tg_booking
    from app.models import Lead
    l = Lead(status="бронь оплачена", booking_date=date(2026, 12, 5), booking_time="14:30", booking_room="Барселона",
             package="Медиум + Премиум", phone_norm="79629311665", prepaid_amount=5000)
    assert tg_booking.snapshot(l)["package"] == "Медиум + Премиум (согласовано с Татьяной)"
    l.package = "Премиум ×2"
    assert tg_booking.snapshot(l)["package"] == "Премиум ×2"


def test_booking_several_rooms_and_long_party(env):
    """Одна бронь на 2 комнаты с одного времени и на 6 часов: журнал, правила, Telegram."""
    from app import tg_booking
    c = login(env)
    lid = c.post("/api/leads", json={"phone": "+79260001101", "name": "Компания"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"booking_date": "2026-12-02", "booking_time": "10:00",
                                       "booking_room": "Монако + Сингапур", "booking_hours": 6, "booking_show": "Мафия + Тесла-шоу"})
    j = c.get("/api/journal?date=2026-12-02").json()
    mine = [b for b in j["bookings"] if b["id"] == lid]
    assert sorted(b["room"] for b in mine) == ["Монако", "Сингапур"] and all(b["end"] == "16:00" and b["minutes"] == 360 for b in mine)
    assert c.get(f"/api/journal?date=2026-12-02&exclude_id={lid}").json()["bookings"] == []       # выбор брони в журнале
    chk = lambda q: c.get("/api/bookings/check?date=2026-12-02&" + q).json()["warnings"]  # noqa: E731
    assert any(w["level"] == "red" for w in chk("room=Монако&time=15:00"))                        # 6 часов: занято до 16:00
    assert any(w["level"] == "red" for w in chk("room=Монако&time=16:15"))                        # перерыв меньше 30 минут — нельзя
    assert any("согласование с Татьяной" in w["text"] for w in chk("room=Монако&time=16:30"))     # 30 минут — по согласованию
    assert not any(w["level"] == "red" for w in chk("room=Монако&time=17:00"))
    assert not any(w["level"] == "red" for w in chk(f"room=Монако%20%2B%20Сингапур&time=10:00&exclude_id={lid}"))  # свои комнаты не мешают
    assert any(w["level"] == "red" for w in chk("room=Барселона&time=10:00"))                     # чужой старт в то же время — нельзя
    lead = c.get(f"/api/leads/{lid}").json()["lead"]
    assert lead["booking_hours"] == 6
    from app.db import session_scope
    from app.models import Lead
    with session_scope() as s:
        l = s.get(Lead, lid)
        l.status, l.prepaid_amount = "бронь оплачена", 10000
        snap = tg_booking.snapshot(l)
    assert snap["room"] == "Монако + Сингапур" and snap["time"] == "10:00–16:00 (6 ч)" and snap["show"] == "Мафия + Тесла-шоу"



def test_booking_without_prepaid_held_24_hours(env):
    """Бронь без предоплаты держится сутки, потом «срок вышел»: другая бронь на это время — жёлтое предупреждение."""
    c = login(env)
    clock.freeze(datetime(2026, 12, 1, 12, 0))
    a = c.post("/api/leads", json={"phone": "+79260001201"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{a}", json={"booking_date": "2026-12-05", "booking_time": "14:00", "booking_room": "Монако"})
    h = c.get(f"/api/leads/{a}").json()["lead"]["hold"]
    assert h["until"] == "2026-12-02T12:00" and not h["expired"] and h["left_hours"] == 24
    chk = lambda: c.get("/api/bookings/check?date=2026-12-05&room=Монако&time=15:00").json()["warnings"]  # noqa: E731
    assert any(w["level"] == "red" for w in chk())                                  # пока держим — занято
    clock.freeze(datetime(2026, 12, 2, 12, 30))
    j = [b for b in c.get("/api/journal?date=2026-12-05").json()["bookings"] if b["id"] == a][0]
    assert j["hold"]["expired"] and not j["paid"]
    w = chk()
    assert w and not any(x["level"] == "red" for x in w) and "сутки прошли" in w[0]["text"]
    c.patch(f"/api/leads/{a}", json={"prepaid_amount": 5000, "status": "бронь оплачена"})
    assert c.get(f"/api/leads/{a}").json()["lead"]["hold"] is None                  # оплатили — держим без срока
    assert any(x["level"] == "red" for x in chk())



def test_refusal_releases_booking_to_archive(env):
    """Отказ или нецелевой: время в журнале освобождается, бронь — в «Удалённые», вернуть можно."""
    c = login(env)
    lid = c.post("/api/leads", json={"phone": "+79260001301"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"booking_date": "2026-12-06", "booking_time": "10:00", "booking_room": "Монако"})
    c.patch(f"/api/leads/{lid}", json={"status": "нецелевой лид"})
    l = c.get(f"/api/leads/{lid}").json()["lead"]
    assert not l["booking_date"] and not l["booking_room"]
    assert [d for d in c.get("/api/bookings/deleted").json() if d["lead_id"] == lid]
    assert not any(w["level"] == "red" for w in c.get("/api/bookings/check?date=2026-12-06&room=Монако&time=10:00").json()["warnings"])



def test_weekend_two_rooms_one_booking_no_chain_warning(env):
    """Суббота: в Сингапуре уже есть 14:00; бронь Монако + Сингапур на 10:00 — без «двум комнатам не разойтись»."""
    c = login(env)
    a = c.post("/api/leads", json={"phone": "+79260001701"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{a}", json={"booking_date": "2026-12-05", "booking_time": "14:00", "booking_room": "Сингапур"})
    w = c.get("/api/bookings/check?date=2026-12-05&room=Монако%20%2B%20Сингапур&time=10:00").json()["warnings"]
    assert w == []
    w = c.get("/api/bookings/check?date=2026-12-05&room=Монако&time=10:00").json()["warnings"]
    assert w and "стартовать одновременно" in w[0]["text"] and "10:30 или 11:00" in w[0]["text"]          # отдельная бронь другой компании — предупреждение остаётся


def test_booking_rooms_with_different_hours(env):
    """Одна бронь: Монако на 6 часов, Сингапур на 3 — журнал, правила и Telegram считают по комнатам."""
    from app import tg_booking
    from app.db import session_scope
    from app.models import Lead
    c = login(env)
    lid = c.post("/api/leads", json={"phone": "+79260001901"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{lid}", json={"booking_date": "2026-12-09", "booking_time": "10:00", "booking_room": "Монако + Сингапур",
                                       "booking_hours": 6, "booking_room_hours": {"Монако": 6, "Сингапур": 3}})
    assert c.get(f"/api/leads/{lid}").json()["lead"]["booking_room_hours"] == {"Монако": 6, "Сингапур": 3}
    ends = {b["room"]: b["end"] for b in c.get("/api/journal?date=2026-12-09").json()["bookings"]}
    assert ends == {"Монако": "16:00", "Сингапур": "13:00"}
    red = lambda q: any(w["level"] == "red" for w in c.get("/api/bookings/check?date=2026-12-09&" + q).json()["warnings"])  # noqa: E731
    assert red("room=Монако&time=14:30") and not red("room=Сингапур&time=14:30")       # Сингапур свободен после 14:00
    assert not red(f"room=Монако%20%2B%20Сингапур&time=10:00&hours=Монако:6,Сингапур:3&exclude_id={lid}")
    with session_scope() as s:
        l = s.get(Lead, lid)
        l.status, l.prepaid_amount = "бронь оплачена", 5000
        snap = tg_booking.snapshot(l)
    assert snap["room"] == "Монако (6 ч) + Сингапур (3 ч)" and snap["time"] == "10:00–16:00 (6 ч)"



def test_short_gap_goes_to_telegram(env, monkeypatch):
    """Перерыв 30 минут между праздниками: бронь ставится, в Telegram — «согласовано с Татьяной»."""
    from app import tg_booking
    sent = []
    monkeypatch.setenv("ZAYAVKI_TG_BOT_TOKEN", "t")
    monkeypatch.setenv("ZAYAVKI_TG_BOOKING_CHAT", "-1")
    monkeypatch.setattr(tg_booking, "send", lambda lead_id, text, old_id=None, old_text=None, cancel=False: sent.append(text))
    c = login(env)
    a = c.post("/api/leads", json={"phone": "+79260002001"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{a}", json={"booking_date": "2026-12-10", "booking_time": "10:00", "booking_room": "Монако"})
    b = c.post("/api/leads", json={"phone": "+79260002002"}).json()["lead"]["id"]
    c.patch(f"/api/leads/{b}", json={"booking_date": "2026-12-10", "booking_time": "13:30", "booking_room": "Монако",
                                     "status": "бронь оплачена", "prepaid_amount": 5000})
    assert "Перерыв между праздниками: 30 мин (согласовано с Татьяной)" in sent[-1]
    c.patch(f"/api/leads/{a}", json={"status": "бронь оплачена", "prepaid_amount": 5000})
    assert "Перерыв между праздниками" in sent[-1]                                 # и у соседней брони


def test_svodka_sold_vs_party_month(env):
    """Бронь оформлена в ноябре на праздник в декабре: в ноябре — «оформлено», в декабре — «праздников в месяце»."""
    c = login(env)
    olga = make_manager(c, "olga12", "Ольга")
    clock.freeze(datetime(2026, 11, 20, 12, 0))
    a = c.post("/api/leads", json={"phone": "+79260002201", "manager_id": olga}).json()["lead"]["id"]
    c.patch(f"/api/leads/{a}", json={"status": "бронь оплачена", "prepaid_amount": 5000,
                                     "booking_date": "2026-12-05", "booking_time": "10:00", "booking_room": "Монако"})
    b = c.post("/api/leads", json={"phone": "+79260002202", "manager_id": olga}).json()["lead"]["id"]
    c.patch(f"/api/leads/{b}", json={"booking_date": "2026-12-12", "booking_time": "10:00", "booking_room": "Монако"})
    c.patch(f"/api/leads/{b}", json={"status": "отказ", "refusal_reason": "дорого"})
    nov = c.get("/api/manager-stats?month=2026-11").json()["cur"]
    dec = c.get("/api/manager-stats?month=2026-12").json()["cur"]
    assert (nov["total"]["sold"], nov["total"]["sold_sum"], nov["total"]["party"]) == (1, 5000, 0)
    assert (dec["total"]["sold"], dec["total"]["party"], dec["total"]["leads"]) == (0, 1, 0)
    assert next(m for m in dec["managers"] if m["name"] == "Ольга")["party"] == 1
    assert nov["refusals"] == [["дорого", 1]] and nov["sources"][0]["leads"] == 2


def test_roles_senior_and_director(env):
    """Старший менеджер ведёт расписание и работает с заявками как менеджер; администратор расписание только смотрит;
    директор может всё, как разработчик."""
    c = login(env)
    for lg, role in (("sen1", "senior"), ("adm1", "admin"), ("dir1", "director")):
        assert c.post("/api/users", json={"login": lg, "name": lg, "role": role, "password": "pass123"}).status_code == 200
    sen, adm, dr = login(env, "sen1", "pass123"), login(env, "adm1", "pass123"), login(env, "dir1", "pass123")
    sid = next(u["id"] for u in c.get("/api/users").json() if u["login"] == "sen1")
    shift = {"manager_id": sid, "days": ["2026-12-01"], "on": True}
    assert sen.post("/api/shifts/bulk", json=shift).status_code == 200                       # старший — ведёт расписание
    assert adm.post("/api/shifts/bulk", json=shift).status_code == 403                       # администратор — только смотрит
    assert adm.get("/api/shifts?month=2026-12").status_code == 200
    assert sid in [m["id"] for m in c.get("/api/meta").json()["managers"]]                  # старший получает заявки
    lid = c.post("/api/leads", json={"phone": "+79260002301", "manager_id": sid}).json()["lead"]["id"]
    assert sen.get(f"/api/leads/{lid}").status_code == 200 and sen.get("/api/users").status_code == 403
    assert dr.get("/api/manager-stats?month=2026-11").status_code == 200                    # директор — как разработчик
    assert dr.post(f"/api/leads/{lid}/delete").status_code == 200
    assert adm.post("/api/users", json={"login": "x1", "name": "x", "role": "director", "password": "pass123"}).status_code == 403
