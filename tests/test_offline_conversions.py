"""Брони в Метрике: статусы загрузок, проверка цели и сводка для карточки на дашборде."""
from datetime import date, datetime, timedelta

from partyhub.collectors import metrika
from partyhub.models import ConversionUpload, CrmRecord, Touch
from partyhub.pipeline.merge import rebuild_leads


class Client:
    counter_ids = ["111"]

    def __init__(self, status="UPLOADED", goals=None):
        self.rows, self.status, self._goals, self.asked = [], status, goals or [], []

    def upload_offline_conversions(self, rows, id_type):
        self.rows += rows
        return {"uploading": {"id": 77, "status": "UPLOADED"}}

    def uploading(self, upload_id):
        self.asked.append(upload_id)
        return {"id": int(upload_id), "status": self.status}

    def goals(self, counter):
        return self._goals


def _booking(session, phone, at, **ids):
    session.add(Touch(source="form", system="site", external_id="f" + phone[-3:], created_at=at, phone=phone, **ids))
    session.add(CrmRecord(entity="booking", external_id=phone[-3:], phone=phone, status="booked", created_at=at))


def test_upload_saves_status_and_refresh_updates_it(session, fixed_now, monkeypatch):
    monkeypatch.setattr(metrika, "now_local", lambda: fixed_now)
    _booking(session, "+79990000101", datetime(2026, 10, 15, 12), ym_client_id="c1")
    session.flush()
    rebuild_leads(session)
    c = Client(status="PROCESSED")
    assert metrika.upload_conversions(session, client=c)["uploaded"] == 1
    assert session.query(ConversionUpload).one().status == "UPLOADED"
    assert metrika.refresh_upload_statuses(session, client=c)["checked"] == 1
    assert session.query(ConversionUpload).one().status == "PROCESSED"
    # итоговый статус больше не спрашиваем
    assert metrika.refresh_upload_statuses(session, client=c)["checked"] == 0
    assert c.asked == ["77"]


def test_goal_check_finds_identifier(session, fixed_now, monkeypatch):
    monkeypatch.setattr(metrika, "now_local", lambda: fixed_now)
    c = Client(goals=[{"id": "5", "name": "Заявка", "identifier": "lead"}])
    assert metrika.check_offline_goal(session, client=c)["found"] is False
    c._goals.append({"id": "6", "name": "Бронь", "identifier": "booking"})
    res = metrika.check_offline_goal(session, client=c)
    assert res["found"] and res["goal_id"] == "6"
    s = metrika.offline_summary(session, date(2026, 10, 1), date(2026, 10, 20))
    assert s["goal_found"] and s["goal_name"] == "Бронь"


def test_summary_explains_why_bookings_did_not_go(session, fixed_now, monkeypatch):
    monkeypatch.setattr(metrika, "now_local", lambda: fixed_now)
    _booking(session, "+79990000201", datetime(2026, 10, 15, 12), ym_client_id="c2")  # уйдёт
    _booking(session, "+79990000202", datetime(2026, 10, 16, 12))  # без ClientID
    session.flush()
    rebuild_leads(session)
    metrika.upload_conversions(session, client=Client())
    _booking(session, "+79990000203", datetime(2026, 10, 17, 12), yclid="y3")  # ещё не отправлена
    session.flush()
    rebuild_leads(session)
    s = metrika.offline_summary(session, date(2026, 10, 1), date(2026, 10, 20))
    assert (s["bookings"], s["sent"], s["in_progress"], s["no_id"], s["waiting"]) == (3, 1, 1, 1, 1)
    assert s["uploads"][0]["rows"] == 1 and s["uploads"][0]["status"] == "UPLOADED"


def test_window_counts_from_last_identified_touch(session, fixed_now, monkeypatch):
    """Первая заявка давно, но человек недавно снова оставил заявку на сайте — бронь уходит."""
    monkeypatch.setattr(metrika, "now_local", lambda: fixed_now)
    phone = "+79990000301"
    old = fixed_now - timedelta(days=60)
    session.add(Touch(source="form", system="site", external_id="old", created_at=old, phone=phone, ym_client_id="c-old"))
    session.add(Touch(source="form", system="site", external_id="new", created_at=fixed_now - timedelta(days=3), phone=phone,
                      ym_client_id="c-new"))
    session.add(CrmRecord(entity="booking", external_id="301", phone=phone, status="booked", created_at=fixed_now - timedelta(days=2)))
    session.flush()
    rebuild_leads(session)
    items = metrika.pending_conversions(session)
    assert [i["client_id"] for i in items] == ["c-new"]


def test_goals_read_identifier_of_js_event():
    import httpx

    body = {"goals": [{"id": 1, "name": "Бронь", "type": "action", "conditions": [{"type": "exact", "url": "booking"}]},
                      {"id": 2, "name": "Спасибо", "type": "url", "conditions": [{"type": "contain", "url": "/spasibo"}]}]}
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    goals = metrika.MetrikaClient.for_token("t", http).goals("111")
    assert [g["identifier"] for g in goals] == ["booking", ""]
