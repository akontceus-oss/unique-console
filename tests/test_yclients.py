import json
from datetime import date, datetime

import httpx

from partyhub.collectors.yclients import YclientsClient, YclientsCollector, booking_status, parse_records
from partyhub.models import CrmRecord, Touch
from partyhub.pipeline import metrics as M
from partyhub.pipeline.merge import rebuild_leads


def rec(i, phone, created, attendance=0, deleted=False, cost=12000):
    return {"id": i, "client": {"name": f"Клиент {i}", "phone": phone}, "create_date": created, "datetime": "2026-10-25T15:00:00+03:00",
            "visit_attendance": attendance, "deleted": deleted, "services": [{"title": "Заезд", "cost": cost}], "staff": {"name": "Инструктор"}}


def test_booking_status_and_parse():
    assert booking_status({"deleted": True}) == "lost"
    assert booking_status({"visit_attendance": 1}) == "done"
    assert booking_status({"visit_attendance": -1}) == "lost"
    assert booking_status({"visit_attendance": 0, "prepaid_confirmed": 3000}) == "prepaid"
    assert booking_status({"visit_attendance": 2}) == "booked"
    row = parse_records([rec(5, "+7 (916) 123-45-67", "2026-10-20 12:30:00")])[0]
    assert row["entity"] == "booking" and row["phone"] == "+79161234567" and row["amount"] == 12000
    assert row["party_date"] == date(2026, 10, 25) and row["created_at"] == datetime(2026, 10, 20, 12, 30)


def test_client_pages_and_auth_header():
    calls = []

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer P, User U"
        assert request.headers["Accept"] == "application/vnd.yclients.v2+json"
        page = int(request.url.params["page"])
        calls.append((request.url.path, page, request.url.params["c_start_date"]))
        data = [rec(page * 10 + i, "+79160000000", "2026-10-20 10:00:00") for i in range(2 if page == 1 else 1)]
        return httpx.Response(200, json={"success": True, "data": data, "meta": {"count": 3}})

    import partyhub.collectors.yclients as yc

    yc.PAGE, old = 2, yc.PAGE
    try:
        c = YclientsClient("P", "U", http=httpx.Client(transport=httpx.MockTransport(handler)))
        assert len(c.records("77", date(2026, 10, 1), date(2026, 10, 20))) == 3
    finally:
        yc.PAGE = old
    assert calls == [("/api/v1/records/77", 1, "2026-10-01"), ("/api/v1/records/77", 2, "2026-10-01")]


def test_bookings_reconcile_with_direct_and_other_sources(session, monkeypatch, fixed_now):
    monkeypatch.setenv("PHONE_REGION", "RU")
    from partyhub import config

    config.get_settings.cache_clear()
    # обращения: из Директа (РСЯ) и из соцсетей
    session.add_all([
        Touch(source="form", system="tilda", external_id="t1", created_at=datetime(2026, 10, 18, 10), phone="+79161111111",
              utm_source="yandex", utm_medium="cpc", utm_campaign="555", utm_content="context|site.ru"),
        Touch(source="form", system="site", external_id="t2", created_at=datetime(2026, 10, 18, 11), phone="+79162222222",
              utm_source="vk", utm_medium="social"),
    ])

    class Fake:
        def records(self, company, d1, d2):
            return [rec(1, "+79161111111", "2026-10-19 09:00:00", attendance=1),  # пришёл из Директа
                    rec(2, "+79162222222", "2026-10-19 10:00:00"),                 # из соцсетей
                    rec(3, "+79163333333", "2026-10-19 11:00:00"),                 # без обращения — сам
                    rec(4, "+79164444444", "2026-10-19 12:00:00", deleted=True)]   # отменена — не бронь

    monkeypatch.setenv("YCLIENTS_COMPANY_ID", "77")
    config.get_settings.cache_clear()
    assert YclientsCollector(Fake()).collect(session, date(2026, 10, 1), date(2026, 10, 20)) == 4
    assert session.query(CrmRecord).filter_by(entity="booking").count() == 4
    rebuild_leads(session)
    rows = M.booking_reconciliation(session, date(2026, 10, 1), date(2026, 10, 20))
    by = {r["group"]: r for r in rows}
    assert set(by) == {"Директ", "Другие источники", "Без источника"}
    assert by["Директ"]["bookings"] == 1 and by["Директ"]["source"] == "Директ · РСЯ"
    assert by["Другие источники"]["source"] == "Соцсети" and by["Без источника"]["with_touch"] == 0
    assert sum(r["bookings"] for r in rows) == 3 and abs(sum(r["share"] for r in rows) - 1) < 1e-3


def _xlsx(rows) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


EXPORT = [
    ["Записи за период 01.10.2026 – 20.10.2026"],
    [],
    ["ID записи", "Дата создания", "Дата визита", "Время", "Клиент", "Телефон", "Сотрудник", "Услуги", "Стоимость", "Статус"],
    [101, datetime(2026, 10, 19, 9), datetime(2026, 10, 25), "15:00", "Иван", "+7 (916) 111-11-11", "Инструктор", "Заезд", "12 000 ₽", "Клиент пришёл"],
    [102, "19.10.2026 10:00", "25.10.2026 16:00", None, "Мария", "89162222222", "Инструктор", "Заезд", 9000, "Ожидание клиента"],
    [103, "19.10.2026 11:00", "26.10.2026", "12:30", "Пётр", "", "Инструктор", "Заезд", 9000, "Клиент не пришёл"],
    [None, None, None, None, None, None, None, "Итого", 30000, None],
]


def test_export_file_columns_statuses_and_dates(monkeypatch):
    monkeypatch.setenv("PHONE_REGION", "RU")
    from partyhub import config
    from partyhub.collectors.yclients import file_status, find_columns, parse_export

    config.get_settings.cache_clear()
    cols = find_columns(EXPORT[2])
    assert cols["created"] == 1 and cols["visit"] == 2 and cols["time"] == 3 and cols["status"] == 9
    assert file_status("Удалена") == "lost" and file_status("Клиент пришел") == "done" and file_status("Подтвердил") == "booked"
    res = parse_export(_xlsx(EXPORT), "records.xlsx")
    rows = {r["external_id"]: r for r in res["rows"]}
    assert set(rows) == {"101", "102", "103"} and res["skipped"] == 1 and not res["no_created"]
    assert rows["101"]["status"] == "done" and rows["101"]["amount"] == 12000 and rows["101"]["phone"] == "+79161111111"
    assert rows["101"]["party_date"] == date(2026, 10, 25) and rows["101"]["created_at"] == datetime(2026, 10, 19, 9)
    assert rows["102"]["created_at"] == datetime(2026, 10, 19, 10) and rows["103"]["status"] == "lost" and rows["103"]["phone"] is None


def test_export_csv_without_id_or_created_date(monkeypatch):
    monkeypatch.setenv("PHONE_REGION", "RU")
    from partyhub import config
    from partyhub.collectors.yclients import parse_export

    config.get_settings.cache_clear()
    csv = "Дата;Клиент;Телефон;Сумма;Статус\n25.10.2026 15:00;Иван;79161111111;5000,50;Ожидание\n".encode("cp1251")
    res = parse_export(csv, "x.csv")
    assert res["no_created"] and res["rows"][0]["created_at"] == datetime(2026, 10, 25, 15)
    assert res["rows"][0]["amount"] == 5000.5 and res["rows"][0]["external_id"].startswith("f")
    assert parse_export(csv, "x.csv")["rows"][0]["external_id"] == res["rows"][0]["external_id"]  # повторно — тот же ID
    import pytest

    with pytest.raises(ValueError):
        parse_export("Имя;Город\nИван;Москва\n".encode(), "x.csv")


def test_upload_button_imports_without_duplicates(fixed_now):
    from fastapi.testclient import TestClient

    from partyhub.web.app import app

    auth, h = ("admin", "secret"), {"X-Requested-With": "partyhub"}
    with TestClient(app) as c:
        c.post("/api/projects", auth=auth, headers=h, json={"name": "Crazy Drift"})
        c.post("/p/crazy-drift/api/project/settings", auth=auth, headers=h, json={"phone_region": "RU"})
        url = "/p/crazy-drift/api/project/yclients/import"
        files = {"file": ("records.xlsx", _xlsx(EXPORT))}
        assert c.post(url, auth=auth, files=files).status_code == 403  # только из дашборда
        r = c.post(url, auth=auth, headers=h, files=files).json()
        assert r["rows"] == 3 and r["date_from"] == "2026-10-19" and r["without_phone"] == 1
        assert c.post(url, auth=auth, headers=h, files={"file": ("records.xlsx", _xlsx(EXPORT))}).json()["rows"] == 3
        bad = c.post(url, auth=auth, headers=h, files={"file": ("x.csv", "Имя;Город\nИван;Москва\n".encode())})
        assert bad.status_code == 422 and "Телефон" in bad.json()["detail"]
        last = c.get("/p/crazy-drift/api/project/collect", auth=auth).json()["blocks"]["yclients"]
        assert last[0]["collector"] == "yclients_file" and last[0]["rows"] == 3
        from partyhub.db import session_scope
        from partyhub.projects import reset_current, set_current

        tok = set_current("crazy-drift")
        try:
            with session_scope() as s:
                assert s.query(CrmRecord).filter_by(entity="booking").count() == 3
        finally:
            reset_current(tok)


def test_report_with_two_row_header_like_yclients(monkeypatch):
    """«Отчёт по записям» YCLIENTS: шапка в две строки, без ID, метка «ПРЕДОПЛАТА ВНЕСЕНА» в «Категории»."""
    monkeypatch.setenv("PHONE_REGION", "RU")
    from partyhub import config
    from partyhub.collectors.yclients import parse_export

    config.get_settings.cache_clear()
    report = [
        ["Сотрудник", None, "Клиент", None, None, "Время визита", "Создал", None, "Статус", "Источник", "Услуги", None, None, "Категория"],
        ["Имя\t", "Специализация", "Имя", "\tТелефон", "Email", None, "Имя\t", "Дата", None, None, "Название", "Стоимость, ₽",
         "Стоимость с учетом скидки, ₽", None],
        ["Монако", "Зона отдыха", "Ольга", 79251936403, None, "2026-12-23 18:00:00", "Админ", "2026-10-10 11:56:41",
         "Ожидание клиента", "Web-интерфейс", "Пакет", 39000, 34000, "ПРЕДОПЛАТА ВНЕСЕНА"],
        ["Сингапур", "Зона отдыха", "Ирина", 79636777118, None, "2026-11-01 11:00:00", "Админ", "2026-10-19 18:07:34",
         "Клиент пришёл", "Web-интерфейс", "Пакет", 49000, 44000, "ПРЕДОПЛАТА ВНЕСЕНА"],
    ]
    res = parse_export(_xlsx(report), "Отчет_по_записям.xlsx")
    assert res["columns"]["created"] == "Создал Дата" and res["columns"]["phone"] == "Клиент Телефон"
    a, b = res["rows"]
    assert a["phone"] == "+79251936403" and a["created_at"] == datetime(2026, 10, 10, 11, 56, 41) and a["party_date"] == date(2026, 12, 23)
    assert a["status"] == "prepaid" and a["amount"] == 34000 and a["manager"] == "Монако" and a["title"] == "Ольга"
    assert b["status"] == "done" and a["external_id"] != b["external_id"]
