"""Правила брони комнат (ТЗ «Журнал записи»)."""
from datetime import date

from app.booking import Booking, check, parse_hm, slots

SAT = date(2026, 11, 28)   # суббота
WED = date(2026, 11, 25)   # среда


def B(room, t, i=1):
    return Booking(i, room, parse_hm(t))


def levels(w):
    return [x["level"] for x in w]


def test_parse_hm():
    assert parse_hm("14:30") == 870 and parse_hm("9.05") == 545 and parse_hm("в обед") is None and parse_hm("25:00") is None


def test_room_needs_hour_after_party():
    day = [B("Монако", "11:00")]                       # 11:00–14:00
    assert levels(check(day, WED, "Монако", parse_hm("15:00"))) == []
    w = check(day, WED, "Монако", parse_hm("14:30"))     # перерыв 30 мин
    assert "red" in levels(w) and "нужен час" in w[0]["text"]
    assert "занята" in check(day, WED, "Монако", parse_hm("12:00"))[0]["text"]
    # и до следующего праздника тоже час
    assert "red" in levels(check([B("Монако", "15:00")], WED, "Монако", parse_hm("11:30")))


def test_rooms_start_30_min_apart():
    day = [B("Монако", "11:00")]
    assert "red" in levels(check(day, WED, "Сингапур", parse_hm("11:15")))
    assert "red" in levels(check(day, WED, "Сингапур", parse_hm("11:00")))
    assert "red" not in levels(check(day, WED, "Сингапур", parse_hm("11:30")))


def test_opening_hours():
    assert "red" in levels(check([], WED, "Монако", parse_hm("19:30")))   # кончится в 22:30
    assert "red" in levels(check([], WED, "Монако", parse_hm("09:30")))
    assert check([], WED, "Монако", parse_hm("19:00")) == []


def test_weekend_full_grid_fits_nine_parties():
    """Лесенка 10:00/10:30/11:00 с шагом 4 часа: 9 праздников без единого предупреждения."""
    day = []
    for i, (room, first) in enumerate((("Сингапур", 600), ("Монако", 630), ("Барселона", 660))):
        for k in range(3):
            t = first + 240 * k
            assert check(day, SAT, room, t) == [], (room, t)
            day.append(Booking(i * 3 + k, room, t))
    assert len(day) == 9


def test_weekend_chain_set_by_first_booking():
    day = [B("Монако", "14:30")]                       # Монако → цепочка 10:30/14:30/18:30
    assert check(day, SAT, "Монако", parse_hm("18:30")) == []
    assert "yellow" in levels(check(day, SAT, "Монако", parse_hm("19:00")))   # другая цепочка
    assert "yellow" in levels(check(day, SAT, "Барселона", parse_hm("10:30")))  # цепочка занята Монако
    assert check(day, SAT, "Барселона", parse_hm("11:00")) == []
    w = check(day, SAT, "Барселона", parse_hm("12:00"))
    assert levels(w) == ["yellow"] and "вне сетки" in w[0]["text"]


def test_weekday_dead_window():
    # бронь в 13:00 оставляет утром 3 часа (10:00–13:00): праздник с часом перерыва не влезет
    w = check([], WED, "Монако", parse_hm("13:00"))
    assert levels(w) == ["yellow"] and "10:00–13:00" in w[0]["text"]
    # 11:00 и 19:00 — между ними ровно место под праздник 15:00–18:00 с часом перерыва с обеих сторон
    assert check([B("Монако", "11:00")], WED, "Монако", parse_hm("19:00")) == []
    # 11:00 и 18:00 — праздник 15:00 уже не влезает
    assert "yellow" in levels(check([B("Монако", "11:00")], WED, "Монако", parse_hm("18:00")))


def test_slots_mark_grid_and_busy():
    sl = {x["time"]: x for x in slots([B("Монако", "14:30")], SAT, "Барселона")}
    assert sl["11:00"]["grid"] and sl["11:00"]["level"] is None
    assert sl["14:30"]["level"] == "red"           # тот же старт, что у Монако
    assert sl["12:00"]["level"] == "yellow" and not sl["12:00"]["grid"]
