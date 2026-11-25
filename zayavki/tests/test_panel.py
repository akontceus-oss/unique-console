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
    assert lead["source"] == "Реклама · Яндекс Директ" and lead["utm"]["campaign"] == "dr"

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
    assert lead["source"] == "Реклама · Яндекс Директ" and "/7-let" in lead["admin_comment"]
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
    assert by_phone["79260000011"]["source_group"] == "Поиск и карты" and by_phone["79260000011"]["source_name"] == "2ГИС"
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
    assert lena.get("/api/bookings/deleted").json() == [] and lena.post(f"/api/leads/{a}/booking/delete").status_code == 404
