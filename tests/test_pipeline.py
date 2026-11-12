from datetime import date, datetime, timedelta

from partyhub.models import AdStat, CrmRecord, Lead, SeoKeyword, SeoPosition, Touch
from partyhub.pipeline import metrics as M
from partyhub.pipeline.merge import rebuild_leads
from partyhub.pipeline.normalize import mask_phone, normalize_phone, parse_dt, parse_utm_content, source_label


# ---------------------------------------------------------------- нормализация
def test_phone_normalization_armenia_and_russia():
    assert normalize_phone("091 23-45-67") == "+37491234567"
    assert normalize_phone("+374 (91) 234567") == "+37491234567"
    assert normalize_phone("+7 916 123-45-67") == "+79161234567"
    assert normalize_phone("") is None
    assert normalize_phone("12") is None


def test_mask_phone_hides_middle_digits():
    assert mask_phone("+37491234567") == "+374 9•••••67"
    assert mask_phone(None) == "—"


def test_utm_content_parsing():
    assert parse_utm_content("context|games.example") == ("AD_NETWORK", "games.example")
    assert parse_utm_content("search|none") == ("SEARCH", None)
    assert parse_utm_content("") == (None, None)
    assert parse_utm_content("Context|Mama-Club.AM") == ("AD_NETWORK", "mama-club.am")


def test_source_labels():
    assert source_label("yandex", "cpc", "AD_NETWORK") == "Директ · РСЯ"
    assert source_label("yandex", "cpc", "SEARCH") == "Директ · Поиск"
    assert source_label("google", "organic", None) == "SEO"
    assert source_label("instagram", "social", None) == "Соцсети"
    assert source_label("", "", None) == "Прямые / неизвестно"


def test_parse_dt_formats():
    assert parse_dt("15.01.2026 10:20:30") == datetime(2026, 1, 15, 10, 20, 30)
    # Битрикс отдаёт время с часовым поясом — переводим в часовой пояс бизнеса (Ереван, UTC+4)
    assert parse_dt("2026-01-15T10:00:00+03:00") == datetime(2026, 1, 15, 11, 0)
    assert parse_dt("мусор") is None


# ---------------------------------------------------------------- склейка
def _touch(session, ext, at, phone, source="call", **kw):
    t = Touch(source=source, system="test", external_id=ext, created_at=at, phone=phone, **kw)
    session.add(t)
    return t


def test_touches_with_same_phone_merge_into_one_lead(session):
    t0 = datetime(2026, 10, 10, 12, 0)
    _touch(session, "1", t0, "+37491000001", utm_source="yandex", utm_medium="cpc", utm_campaign="701", utm_content="context|kids.am")
    _touch(session, "2", t0 + timedelta(hours=3), "+37491000001", source="chat")
    _touch(session, "3", t0 + timedelta(days=45), "+37491000001", source="form")  # за окном склейки — новая заявка
    session.add(CrmRecord(entity="deal", external_id="d1", phone="+37491000001", status="booked", status_raw="PREPAYMENT_INVOICE",
                          amount=90000, created_at=t0 + timedelta(minutes=10), party_date=date(2026, 10, 25), guests=10))
    session.flush()
    assert rebuild_leads(session) == 2
    first, second = session.query(Lead).order_by(Lead.first_touch_at).all()
    assert first.touches_count == 2
    assert first.source_label == "Директ · РСЯ"
    assert first.placement == "kids.am" and first.campaign_id == 701
    assert first.status == "booked" and float(first.amount) == 90000
    assert first.party_date == date(2026, 10, 25)
    assert second.status == "new"  # сделка относится к первой заявке, а не ко второй


def test_deal_beats_lead_and_crm_without_touches_becomes_lead(session):
    t0 = datetime(2026, 10, 10, 12, 0)
    _touch(session, "1", t0, "+37491000002")
    session.add_all([
        CrmRecord(entity="lead", external_id="l1", phone="+37491000002", status="in_progress", created_at=t0),
        CrmRecord(entity="deal", external_id="d1", phone="+37491000002", status="done", amount=120000, created_at=t0 + timedelta(days=1)),
        CrmRecord(entity="lead", external_id="l2", phone="+37491000099", status="booked", created_at=t0, utm_source="instagram"),
    ])
    session.flush()
    assert rebuild_leads(session) == 2
    called = session.query(Lead).filter_by(phone="+37491000002").one()
    assert called.status == "done" and called.crm_external_id == "deal:d1"
    walk_in = session.query(Lead).filter_by(phone="+37491000099").one()
    assert walk_in.channel == "crm" and walk_in.source_label == "Соцсети"


def test_missed_call_flag(session):
    t0 = datetime(2026, 10, 10, 12, 0)
    _touch(session, "1", t0, "+37491000003", answered=False)
    _touch(session, "2", t0 + timedelta(minutes=5), "+37491000003", answered=False)
    session.flush()
    rebuild_leads(session)
    assert session.query(Lead).one().missed_call is True


def test_rebuild_is_idempotent(session):
    t0 = datetime(2026, 10, 10, 12, 0)
    for i in range(5):
        _touch(session, str(i), t0 + timedelta(hours=i), f"+3749100001{i}")
    session.flush()
    assert rebuild_leads(session) == 5
    assert rebuild_leads(session) == 5


# ---------------------------------------------------------------- метрики
def test_summary_cpl_and_cpb(session):
    d = date(2026, 10, 10)
    session.add(AdStat(date=d, campaign_id=701, campaign_name="Поиск", network="SEARCH", placement="search", impressions=1000, clicks=50, cost=30000))
    t0 = datetime(2026, 10, 10, 12, 0)
    for i in range(5):
        _touch(session, str(i), t0 + timedelta(minutes=i), f"+3749100002{i}", utm_source="yandex", utm_medium="cpc", utm_campaign="701", utm_content="search|none")
    session.add(CrmRecord(entity="deal", external_id="d", phone="+37491000020", status="done", amount=100000, created_at=t0))
    _touch(session, "seo", t0, "+37491000030", utm_source="google", utm_medium="organic")
    session.flush()
    rebuild_leads(session)
    s = M.summary(session, d, d)["totals"]
    assert s["leads_direct"] == 5 and s["leads_all"] == 6
    assert s["cpl"] == 6000 and s["bookings_direct"] == 1 and s["cpb"] == 30000
    assert s["romi"] == round((100000 - 30000) / 30000, 4)


def test_party_date_falls_back_to_form(session):
    _touch(session, "f", datetime(2026, 10, 10, 12), "+37491000040", source="form", extra={"party_date": "2026-11-05"})
    session.flush()
    rebuild_leads(session)
    assert session.query(Lead).one().party_date == date(2026, 11, 5)


def test_seo_keyword_outside_serp_has_empty_url(session):
    # запрос вне выдачи: позиции и адреса нет; pandas превращает None в NaN, который ломал JSON ответа
    session.add(SeoPosition(date=date(2026, 10, 26), keyword="вне выдачи", region="1:2", region_name="Москва · Google", position=None, url=None))
    session.add(SeoPosition(date=date(2026, 10, 26), keyword="в топе", region="1:2", region_name="Москва · Google", position=1, url="https://site.ru/"))
    session.flush()
    res = M.seo(session, date(2026, 10, 1), date(2026, 10, 28))
    urls = {r["keyword"]: r["url"] for r in res["keywords"]}
    assert urls == {"вне выдачи": "", "в топе": "https://site.ru/"}
    import json
    json.dumps(res, allow_nan=False)


def test_seo_regions_growth_and_importance(session):
    d1, d2 = date(2026, 10, 19), date(2026, 10, 26)
    ya, go = ("1:33", "Москва · Yandex · моб."), ("1:2", "Москва · Google")
    data = {  # запрос: (частота Яндекса, {регион: (позиция неделю назад, сейчас)})
        "день рождения ребенка": (5000, {ya: (9, 6), go: (12, 15)}),
        "картинг внуково": (300, {ya: (1, 1), go: (2, 2)}),
        "квест для детей": (50, {ya: (None, None), go: (40, 35)}),
    }
    for kw, (vol, cells) in data.items():
        session.add(SeoKeyword(project="P", keyword=kw, volume_yandex=vol, volume_google=vol // 10))
        for (reg, name), (p0, p1) in cells.items():
            for d, p in ((d1, p0), (d2, p1)):
                session.add(SeoPosition(date=d, keyword=kw, region=reg, region_name=name, project="P", group_name="Г", position=p))
    session.flush()
    res = M.seo(session, date(2026, 10, 1), d2)
    regs = {r["region"]: r for r in res["regions"]}
    assert [r["engine"] for r in res["regions"]] == ["yandex", "google"]  # Яндекс первым
    assert regs["1:33"]["top10"] == 2 and regs["1:33"]["prev_date"] == "2026-10-19" and regs["1:33"]["total"] == 3
    assert 0 < regs["1:33"]["visibility"] < 1 and regs["1:33"]["visibility"] > regs["1:33"]["prev_visibility"]  # частый запрос поднялся
    top = res["growth"][0]
    assert (top["keyword"], top["region"], top["stage"]) == ("день рождения ребенка", "1:33", "near")
    assert {g["keyword"] for g in res["growth"]} == {"день рождения ребенка", "квест для детей"}  # из топ-3 расти некуда
    kw = {k["keyword"]: k for k in res["keywords"]}
    assert kw["день рождения ребенка"]["cells"][0]["change"] == 3 and kw["день рождения ребенка"]["cells"][1]["change"] == -3
    assert kw["квест для детей"]["cells"][0]["hist"] == [None, None]  # проверялся, но не в топ-100
    assert res["keywords"][0]["keyword"] == "день рождения ребенка" and all(k["important"] for k in res["keywords"])
    import json
    json.dumps(res, allow_nan=False, default=str)


def test_goal_totals_sum_goal_dicts():
    assert M.goal_totals([{"1": 2}, None, {"1": 1, "2": 4}]) == {"1": 3, "2": 4}


def test_old_database_gets_new_not_null_columns_with_default(session):
    """База, созданная до появления source_channel/source_method: столбцы добавляются, старые лиды читаются."""
    from sqlalchemy import inspect, text

    from partyhub import db

    t = datetime(2026, 10, 1, 10)
    session.add(Lead(phone="+70000000000", first_touch_at=t, last_touch_at=t, channel="call", source_label="SEO"))
    session.commit()
    eng = db.get_engine()
    with eng.begin() as c:
        c.execute(text("ALTER TABLE leads DROP COLUMN source_channel"))
        c.execute(text("ALTER TABLE leads DROP COLUMN source_method"))
    db.add_missing_columns()
    cols = {c["name"] for c in inspect(eng).get_columns("leads")}
    assert {"source_channel", "source_method"} <= cols
    session.expire_all()
    old = session.query(Lead).filter_by(phone="+70000000000").one()
    assert old.source_channel == "" and old.source_method == ""
