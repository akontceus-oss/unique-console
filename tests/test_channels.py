"""Заявки и брони по источникам: UTM → колтрекинг → тег панели заявок."""
from datetime import date, datetime

import httpx

from partyhub.collectors.callibri import parse_statistics
from partyhub.collectors.zayavki import ZayavkiClient, ZayavkiCollector, panel_status, parse_leads
from partyhub.db import session_scope
from partyhub.models import CrmRecord, Lead, Touch
from partyhub.pipeline import channels as C
from partyhub.pipeline import metrics as M
from partyhub.pipeline.merge import rebuild_leads


def test_words_and_utm():
    assert C.from_words("Реклама · Яндекс Директ") == C.DIRECT
    assert C.from_words("реклама яндекса") == C.DIRECT
    assert C.from_words("Были на чужом празднике") == C.FRIENDS
    assert C.from_words("Друзья, подруга") == C.FRIENDS
    assert C.from_words("2ГИС") == C.GIS and C.from_words("2gis статичный") == C.GIS
    assert C.from_words("Реклама на Ozon") == C.OZON
    assert C.from_words("Instagram") == C.INSTAGRAM
    assert C.from_words("Поиск Яндекса") == C.SEARCH
    assert C.from_words("Яндекс Карты") == C.MAPS
    assert C.from_words("Прямые заходы") is None
    assert C.from_utm("yandex", "cpc") == C.DIRECT
    assert C.from_utm("yandex", "organic") == C.SEARCH
    assert C.from_utm("instagram", "social") == C.INSTAGRAM
    assert C.from_utm("2gis", "referral") == C.GIS
    assert C.from_utm("ozon", "cpc") == C.OZON
    assert C.from_utm("", "") is None
    assert C.split_tag("Знакомые · Друзья, подруга") == ("Друзья, подруга", "Знакомые")


class T:
    def __init__(self, utm_source="", utm_medium="", system="site", extra=None):
        self.utm_source, self.utm_medium, self.system, self.extra = utm_source, utm_medium, system, extra


def test_classify_priority():
    tag = T(system="zayavki", extra={"tag": "Были на чужом празднике", "tag_group": "Знакомые"})
    call = T(system="callibri", extra={"callibri_channel": "2ГИС"})
    assert C.classify([T("yandex", "cpc"), call, tag]) == (C.DIRECT, C.METHOD_UTM)
    assert C.classify([call, tag]) == (C.GIS, C.METHOD_CALL)
    assert C.classify([tag]) == (C.FRIENDS, C.METHOD_TAG)
    # тег по группе, если название канала ничего не говорит
    assert C.classify([T(system="zayavki", extra={"tag": "Вывеска у ТЦ", "tag_group": "Уже знают нас"})]) == (C.RETURNING, C.METHOD_TAG)
    # непонятная метка уступает тегу
    assert C.classify([T("partner_x", ""), tag]) == (C.FRIENDS, C.METHOD_TAG)
    assert C.classify([T("partner_x", "")]) == (C.OTHER, C.METHOD_UTM)
    assert C.classify([T()]) == (C.NONE, "")


def test_callibri_channel_from_block():
    payload = {"channels_statistics": [
        {"name_channel": "2ГИС", "calls": [{"date": "2026-11-02 12:00:00", "phone": "+37491000001", "name": "Анна", "duration": 60}]},
        {"name_channel": "Яндекс.Директ", "calls": [{"date": "2026-11-02 13:00:00", "phone": "+37491000002", "duration": 0}]},
    ]}
    touches = parse_statistics(payload)
    assert [t.extra.get("callibri_channel") for t in touches] == ["2ГИС", "Яндекс.Директ"]
    assert touches[0].name == "Анна"


def panel_item(i, phone, created, status="думает", source="Знакомые · Были на чужом празднике", utm=None, **kw):
    return {"id": i, "created_at": created, "updated_at": created, "channel": "manual", "phone_norm": phone, "name": f"Клиент {i}",
            "source": source, "utm": utm or {"source": "", "medium": "", "campaign": "", "content": "", "term": ""},
            "manager": "Ольга", "status": status, "refusal_reason": "", "package": "", "booking_date": kw.get("booking_date"),
            "prepaid_at": kw.get("prepaid_at"), "prepaid_amount": kw.get("prepaid_amount"), "holiday_date": None}


def test_panel_status():
    assert panel_status({"status": "бронь оплачена"}) == "prepaid"
    assert panel_status({"status": "думает", "booking_date": "2026-11-20"}) == "booked"
    assert panel_status({"status": "Далеко"}) == "lost"
    assert panel_status({"status": "нецелевой лид"}) == "spam"
    assert panel_status({"status": "новая"}) == "new"


def test_parse_leads_filters_period_and_skips_crm_without_phone():
    items = [panel_item(1, "79161234567", "2026-11-02T12:00:00"), panel_item(2, "", "2026-11-02T13:00:00"),
             panel_item(3, "79161230000", "2026-10-01T10:00:00")]
    touches, records = parse_leads(items, date(2026, 11, 1), date(2026, 11, 3))
    assert [t.external_id for t in touches] == ["lead:1", "lead:2"]
    assert touches[0].phone == "+79161234567"
    assert touches[0].extra["tag"] == "Были на чужом празднике" and touches[0].extra["tag_group"] == "Знакомые"
    assert [r.external_id for r in records] == ["1"]


def test_collector_end_to_end_dashboard_rows():
    items = [
        panel_item(1, "79160000001", "2026-11-02T12:00:00", status="бронь оплачена", prepaid_amount=5000),
        panel_item(2, "79160000002", "2026-11-02T13:00:00", source="Поиск и карты · 2ГИС"),
        panel_item(3, "79160000003", "2026-11-02T14:00:00", status="бронь оплачена", source="Реклама · Яндекс Директ"),
        panel_item(4, "79160000004", "2026-11-02T15:00:00", source="Реклама · Реклама на Ozon",
                   utm={"source": "yandex", "medium": "cpc", "campaign": "1", "content": "search|none", "term": ""}),
    ]
    seen = {}

    def handler(request: httpx.Request):
        seen["token"] = request.headers["X-Export-Token"]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"now": "2026-11-03T10:00:00", "leads": items})

    client = ZayavkiClient("https://zayavki.example", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with session_scope() as s:
        # звонок с 2ГИС-номера того же клиента — колтрекинг важнее тега
        s.add(Touch(source="call", system="callibri", external_id="calls:1", created_at=datetime(2026, 11, 2, 12, 55),
                    phone="+79160000002", extra={"callibri_channel": "2ГИС"}))
        assert ZayavkiCollector(client).collect(s, date(2026, 11, 1), date(2026, 11, 3)) == 4
        assert ZayavkiCollector(client).collect(s, date(2026, 11, 1), date(2026, 11, 3)) == 4  # повтор без дублей
        assert s.query(CrmRecord).filter_by(entity="panel").count() == 4
        rebuild_leads(s)
        leads = {l.phone: l for l in s.query(Lead)}
        assert len(leads) == 4
        assert (leads["+79160000001"].source_channel, leads["+79160000001"].source_method) == (C.FRIENDS, "tag")
        assert leads["+79160000001"].status == "prepaid"
        assert (leads["+79160000002"].source_channel, leads["+79160000002"].source_method) == (C.GIS, "call")
        assert (leads["+79160000004"].source_channel, leads["+79160000004"].source_method) == (C.DIRECT, "utm")
        rows = {r["source"]: r for r in M.channels(s, date(2026, 11, 1), date(2026, 11, 3))}
    assert seen == {"token": "tok", "params": {"created_from": "2026-11-01", "created_to": "2026-11-03"}}
    assert rows[C.DIRECT]["leads"] == 2 and rows[C.DIRECT]["bookings"] == 1
    assert rows[C.DIRECT]["by_utm"] == 1 and rows[C.DIRECT]["by_tag"] == 1 and rows[C.DIRECT]["bookings_by_tag"] == 1
    assert rows[C.FRIENDS]["bookings"] == 1 and rows[C.FRIENDS]["conversion"] == 1
    assert rows[C.GIS]["by_call"] == 1 and rows[C.GIS]["bookings"] == 0


def test_yclients_booking_without_touch_has_no_source():
    with session_scope() as s:
        s.add(CrmRecord(entity="booking", external_id="b1", phone="+79160000009", status="booked", created_at=datetime(2026, 11, 2, 10)))
        rebuild_leads(s)
        rows = M.channels(s, date(2026, 11, 1), date(2026, 11, 3))
    assert rows == [dict(rows[0], source=C.NONE, leads=1, bookings=1)]
