import json
from datetime import date

import httpx
import pytest

from partyhub.collectors.bitrix import BitrixClient, BitrixCollector
from partyhub.collectors.callibri import parse_statistics
from partyhub.collectors.direct import DirectClient, DirectCollector, DirectError, diff_snapshots, parse_tsv, record_changes
from partyhub.collectors.metrika import MetrikaClient, MetrikaCollector
from partyhub.collectors.topvisor import TopvisorClient, TopvisorCollector, parse_history
from partyhub.models import AdStat, CampaignSnapshot, CrmRecord, SearchQuery, SeoKeyword, SeoPosition, SettingsChange, Touch, WebStat


# ----------------------------------------------------------------- Директ
def test_parse_tsv_treats_dashes_as_empty():
    rows = parse_tsv("Date\tPlacement\tCost\n2026-10-20\t--\t100.5\n")
    assert rows == [{"Date": "2026-10-20", "Placement": None, "Cost": "100.5"}]


def test_report_waits_for_offline_report_then_parses():
    calls = {"n": 0}

    def handler(request: httpx.Request):
        if request.url.path.endswith("/reports"):
            calls["n"] += 1
            assert request.headers["Authorization"] == "Bearer T"
            assert request.headers["Client-Login"] == "client"
            if calls["n"] == 1:
                return httpx.Response(201, headers={"retryIn": "2"})
            return httpx.Response(200, text="Date\tCampaignId\tCost\n2026-10-20\t1\t10\n")
        raise AssertionError(request.url)

    slept = []
    c = DirectClient("T", "client", sandbox=True, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append)
    rows = c.report("CUSTOM_REPORT", ["Date", "CampaignId", "Cost"], date(2026, 10, 20), date(2026, 10, 20))
    assert rows == [{"Date": "2026-10-20", "CampaignId": "1", "Cost": "10"}] and slept == [2]
    assert "api-sandbox" in c.base


def test_api_error_is_raised():
    def handler(request):
        return httpx.Response(200, json={"error": {"error_code": 53, "error_string": "Authorization error", "error_detail": "bad token"}})

    c = DirectClient("T", sandbox=False, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(DirectError) as e:
        c.get_campaigns()
    assert e.value.code == 53


def test_direct_collector_writes_stats_queries_and_snapshot(session):
    ad_tsv = ("Date\tCampaignId\tCampaignName\tAdGroupId\tAdGroupName\tAdNetworkType\tPlacement\tImpressions\tClicks\tCost\n"
              "2026-10-20\t701\tПоиск\t11\tГ1\tSEARCH\t--\t1000\t50\t20000\n"
              "2026-10-20\t703\tРСЯ\t31\tГ3\tAD_NETWORK\tMama-Club.am\t5000\t40\t4000\n")
    q_tsv = "Date\tCampaignId\tCampaignName\tAdGroupId\tQuery\tImpressions\tClicks\tCost\n2026-10-20\t701\tПоиск\t11\tкартинг вакансии\t10\t1\t400\n"

    def handler(request):
        if request.url.path.endswith("/reports"):
            body = json.loads(request.content)
            if {"Device", "Gender"} & set(body["params"]["FieldNames"]):
                return httpx.Response(200, text="")  # срезы для корректировок проверяются отдельно
            return httpx.Response(200, text=q_tsv if body["params"]["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT" else ad_tsv)
        body = json.loads(request.content)
        assert body["method"] == "get"
        return httpx.Response(200, json={"result": {"Campaigns": [
            {"Id": 703, "Name": "РСЯ", "State": "ON", "Status": "ACCEPTED", "DailyBudget": {"Amount": 6_000_000_000},
             "ExcludedSites": {"Items": ["b.example", "a.example"]}, "NegativeKeywords": None,
             "TextCampaign": {"BiddingStrategy": {"Search": {"BiddingStrategyType": "SERVING_OFF"}}}}]}})

    client = DirectClient("T", http=httpx.Client(transport=httpx.MockTransport(handler)))
    n = DirectCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20))
    assert n == 4
    rsy = session.query(AdStat).filter_by(network="AD_NETWORK").one()
    assert rsy.placement == "mama-club.am" and float(rsy.cost) == 4000
    assert session.query(AdStat).filter_by(network="SEARCH").one().placement == "search"
    assert session.query(SearchQuery).one().query == "картинг вакансии"
    snap = session.query(CampaignSnapshot).one()
    assert snap.data["ExcludedSites"] == ["a.example", "b.example"] and snap.data["DailyBudget"] == 6000


def test_snapshot_diff_marks_external_changes(session):
    session.add(CampaignSnapshot(date=date(2026, 10, 19), campaign_id=1, campaign_name="A",
                                 data={"DailyBudget": 5000, "ExcludedSites": ["a.example"], "NegativeKeywords": []}))
    session.add(CampaignSnapshot(date=date(2026, 10, 20), campaign_id=1, campaign_name="A",
                                 data={"DailyBudget": 7000, "ExcludedSites": ["a.example", "b.example"], "NegativeKeywords": []}))
    session.flush()
    assert record_changes(session, date(2026, 10, 20)) == 2
    fields = {c.field: c for c in session.query(SettingsChange)}
    assert fields["DailyBudget"].before == "5000" and fields["DailyBudget"].after == "7000"
    assert fields["ExcludedSites"].after == "добавлено: b.example" and fields["ExcludedSites"].author == "external"


def test_diff_snapshots_lists():
    out = diff_snapshots({"ExcludedSites": ["a", "b"]}, {"ExcludedSites": ["b", "c"]})
    assert ("ExcludedSites", "", "добавлено: c") in out and ("ExcludedSites", "убрано: a", "") in out


# ----------------------------------------------------------------- Метрика
def test_metrika_collector_converts_rates_to_sums(session, monkeypatch):
    monkeypatch.setenv("METRIKA_GOAL_ID", "")

    def handler(request):
        assert request.headers["Authorization"] == "OAuth M"
        if "lastsignTrafficSource" in request.url.params["dimensions"]:
            return httpx.Response(200, json={"total_rows": 0, "data": []})
        return httpx.Response(200, json={"total_rows": 1, "data": [{
            "dimensions": [{"name": "2026-10-20"}, {"name": "yandex"}, {"name": "cpc"}, {"name": "703"}, {"name": "context|kids.am"}, {"name": None}],
            "metrics": [40, 25.0, 90.0]}]})

    client = MetrikaClient("M", "123", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert MetrikaCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20)) == 1
    w = session.query(WebStat).one()
    assert (w.placement, w.visits, w.bounces, w.duration_sum) == ("kids.am", 40, 10, 3600)


def test_metrika_collects_every_site_goal(session, monkeypatch):
    from partyhub import config

    monkeypatch.setenv("METRIKA_GOAL_ID", "11")
    monkeypatch.setenv("METRIKA_GOAL_IDS", "11, 22,33,22")
    config.get_settings.cache_clear()
    seen = {}

    def handler(request):
        if "lastsignTrafficSource" in request.url.params["dimensions"]:
            return httpx.Response(200, json={"total_rows": 0, "data": []})
        seen["metrics"] = request.url.params["metrics"]
        return httpx.Response(200, json={"total_rows": 1, "data": [{
            "dimensions": [{"name": "2026-10-20"}, {"name": "yandex"}, {"name": "cpc"}, {"name": "703"}, {"name": "context|kids.am"}, {"name": None}],
            "metrics": [40, 25.0, 90.0, 3, 0, 2]}]})

    client = MetrikaClient("M", "123", http=httpx.Client(transport=httpx.MockTransport(handler)))
    MetrikaCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20))
    assert seen["metrics"].endswith("ym:s:goal11reaches,ym:s:goal22reaches,ym:s:goal33reaches")
    w = session.query(WebStat).one()
    assert w.goal_reaches == 3 and w.goals == {"11": 3, "33": 2}


def test_metrika_collects_traffic_sources(session, monkeypatch):
    """Для вкладки «Дашборд»: визиты по источникам — поисковик и соцсеть берутся по машинному id."""
    from partyhub import config
    from partyhub.models import TrafficStat

    monkeypatch.setenv("METRIKA_GOAL_ID", "11")
    monkeypatch.setenv("METRIKA_GOAL_IDS", "11")
    config.get_settings.cache_clear()
    seen = {}

    def handler(request):
        dims = request.url.params["dimensions"]
        if "lastsignTrafficSource" not in dims or "lastsignSourceEngine" in dims:  # не «Дашборд»
            return httpx.Response(200, json={"total_rows": 0, "data": []})
        seen["metrics"] = request.url.params["metrics"]
        seen.setdefault("date1", []).append(request.url.params["date1"])
        day = {"name": "2026-10-20"}
        return httpx.Response(200, json={"total_rows": 3, "data": [
            {"dimensions": [day, {"id": "organic", "name": "Переходы из поисковых систем"}, {"id": "yandex", "name": "Яндекс"}, {"name": None}],
             "metrics": [120, 20.0, 4]},
            {"dimensions": [day, {"id": "organic", "name": "Переходы из поисковых систем"}, {"id": "google", "name": "Google"}, {"name": None}],
             "metrics": [50, 30.0, 1]},
            {"dimensions": [day, {"id": "social", "name": "Переходы из социальных сетей"}, {"name": None}, {"id": "instagram", "name": "Instagram"}],
             "metrics": [30, 50.0, 0]},
        ]})

    client = MetrikaClient("M", "123", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert MetrikaCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20)) == 3
    assert seen["metrics"] == "ym:s:visits,ym:s:bounceRate,ym:s:goal11reaches"
    rows = {(t.source, t.engine, t.network): (t.visits, t.bounces, t.goal_reaches) for t in session.query(TrafficStat)}
    assert rows == {("organic", "yandex", ""): (120, 24, 4), ("organic", "google", ""): (50, 15, 1), ("social", "", "instagram"): (30, 15, 0)}
    # первый сбор берёт полгода истории, следующие — только свой период
    MetrikaCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20))
    assert seen["date1"] == ["2026-04-24", "2026-10-20"]


def test_site_goals_fall_back_to_main_and_direct_goals(monkeypatch):
    from partyhub import config

    monkeypatch.setenv("METRIKA_GOAL_IDS", "")
    monkeypatch.setenv("METRIKA_GOAL_ID", "5")
    monkeypatch.setenv("DIRECT_GOAL_IDS", "7,5, x")
    config.get_settings.cache_clear()
    assert config.get_settings().site_goal_ids == ["5", "7"]
    monkeypatch.setenv("METRIKA_GOAL_IDS", ",".join(str(i) for i in range(1, 30)))
    config.get_settings.cache_clear()
    assert len(config.get_settings().site_goal_ids) == config.MAX_SITE_GOALS


# ----------------------------------------------------------------- Callibri
def test_callibri_statistics_to_touches():
    payload = {"channels_statistics": [{
        "name_channel": "Директ",
        "calls": [{"id": 1, "date": "2026-10-20T12:00:00.000+04:00", "phone": "8 (091) 23-45-67", "call_status": "Пропущенный",
                   "utm_source": "yandex", "utm_medium": "cpc", "utm_campaign": "701", "utm_content": "search|none", "metrika_client_id": 555}],
        "feedbacks": [{"id": 2, "date": "20.10.2026 13:00:00", "phone": "+37491234567", "name": "Ани", "form_name": "Праздник"}],
        "chats": [{"id": 3, "date": "2026-10-20T14:00:00+04:00", "ip": "1.1.1.1", "responsible_manager": "Анна"}],
    }]}
    touches = parse_statistics(payload)
    by = {t.source: t for t in touches}
    assert set(by) == {"call", "form", "chat"}
    assert by["call"].answered is False and by["call"].ym_client_id == "555"
    assert by["form"].phone == "+37491234567" and by["form"].extra["form_name"] == "Праздник"
    assert by["chat"].manager == "Анна" and by["chat"].phone is None


# ----------------------------------------------------------------- Битрикс24
def test_bitrix_collector_pages_maps_statuses_and_chat_leads(session, monkeypatch):
    monkeypatch.setenv("BITRIX_PARTY_DATE_FIELD", "UF_CRM_PARTY")
    from partyhub import config

    config.get_settings.cache_clear()
    leads_page1 = {"result": [{"ID": "1", "TITLE": "ДР", "STATUS_ID": "IN_PROCESS", "DATE_CREATE": "2026-10-20T10:00:00+04:00",
                               "DATE_MODIFY": "2026-10-20T11:00:00+04:00", "ASSIGNED_BY_ID": "7", "SOURCE_ID": "4|OPENLINE",
                               "PHONE": [{"VALUE": "+374 91 000001"}], "UTM_SOURCE": "yandex"}], "next": 50}
    leads_page2 = {"result": [{"ID": "2", "TITLE": "Спам", "STATUS_ID": "JUNK", "DATE_CREATE": "2026-10-20T10:00:00+04:00",
                               "DATE_MODIFY": "2026-10-20T11:00:00+04:00", "ASSIGNED_BY_ID": "7", "PHONE": []}]}
    deals = {"result": [{"ID": "9", "TITLE": "Бронь", "STAGE_ID": "C1:EXECUTING", "OPPORTUNITY": "95000", "CONTACT_ID": "5",
                         "DATE_CREATE": "2026-10-20T12:00:00+04:00", "DATE_MODIFY": "2026-10-20T12:30:00+04:00", "ASSIGNED_BY_ID": "7",
                         "UF_CRM_PARTY": "2026-11-02T00:00:00+04:00"}]}

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        body = json.loads(request.content)
        if method == "crm.lead.list":
            return httpx.Response(200, json=leads_page1 if body.get("start", 0) == 0 else leads_page2)
        if method == "crm.deal.list":
            return httpx.Response(200, json=deals)
        if method == "crm.contact.list":
            return httpx.Response(200, json={"result": [{"ID": "5", "PHONE": [{"VALUE": "091000002"}]}]})
        if method == "user.get":
            return httpx.Response(200, json={"result": [{"ID": "7", "NAME": "Анна", "LAST_NAME": "С."}]})
        raise AssertionError(method)

    client = BitrixClient("https://x.bitrix24.am/rest/1/abc/", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert BitrixCollector(client).collect(session, date(2026, 10, 1), date(2026, 10, 20)) == 3
    recs = {r.external_id: r for r in session.query(CrmRecord)}
    assert recs["1"].status == "in_progress" and recs["1"].manager == "Анна С."
    assert recs["2"].status == "spam"
    assert recs["9"].status == "prepaid" and recs["9"].phone == "+37491000002" and float(recs["9"].amount) == 95000
    assert recs["9"].party_date == date(2026, 11, 2)
    chat = session.query(Touch).one()
    assert chat.source == "chat" and chat.phone == "+37491000001"


# ----------------------------------------------------------------- Топвизор
def test_topvisor_history_parsing():
    result = {"keywords": [{"name": "картинг ереван", "positionsData": {
        "2026-10-19:111:1": {"position": "7", "relevant_url": "https://site/"},
        "2026-10-20:111:1": {"position": "--"}}}]}
    rows = sorted(parse_history(result, "Ереван"), key=lambda r: r["date"])
    assert rows[0]["position"] == 7 and rows[1]["position"] is None
    assert rows[0]["region"] == "111:1" and rows[0]["region_name"] == "Ереван"


TV_PROJECTS = [
    {"id": 111, "name": "Картинг", "site": "kart.example", "searchers": [
        {"key": 0, "name": "Яндекс", "regions": [{"index": 1, "name": "Ереван"}, {"index": 2, "name": "Москва"}]}]},
    {"id": 222, "name": "Дни рождения", "site": "party.example", "searchers": [
        {"key": 1, "name": "Google", "regions": [{"index": 5, "name": "Ереван"}]}]},
]


def _topvisor(monkeypatch, project_ids, region_index="", page=1000, keywords=None):
    from partyhub import config
    import partyhub.collectors.topvisor as tv

    monkeypatch.setenv("TOPVISOR_USER_ID", "1")
    monkeypatch.setenv("TOPVISOR_API_KEY", "k")
    monkeypatch.setenv("TOPVISOR_PROJECT_ID", project_ids)
    monkeypatch.setenv("TOPVISOR_REGION_INDEX", region_index)
    config.get_settings.cache_clear()
    monkeypatch.setattr(tv, "PAGE", page)
    calls = []

    def handler(request: httpx.Request):
        assert request.headers["User-Id"] == "1" and request.headers["Authorization"] == "bearer k"
        body = json.loads(request.content)
        if request.url.path.endswith("/projects_2/projects"):
            return httpx.Response(200, json={"result": TV_PROJECTS})
        calls.append(body)
        pid, regions = body["project_id"], body["regions_indexes"]
        kws = [{"name": name, "group_name": "Праздники", **{f: str(100 * (i + 1)) for f in body["fields"] if f.startswith("volume:")}, "positionsData": {
            f"2026-10-20:{pid}:{r}": {"position": str(3 + i), "relevant_url": "https://x/"} for r in regions}}
            for i, name in enumerate(keywords or ["картинг ереван", "день рождения"])]
        return httpx.Response(200, json={"result": {"keywords": kws[body["offset"]:body["offset"] + body["limit"]]}})

    return TopvisorClient("1", "k", http=httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_topvisor_collects_all_projects_and_regions(session, monkeypatch):
    client, calls = _topvisor(monkeypatch, "111, 222")
    assert TopvisorCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20)) == 6  # 2 запроса × 3 региона
    assert [(c["project_id"], c["regions_indexes"]) for c in calls] == [(111, [1, 2]), (222, [5])]
    rows = {(r.keyword, r.region): r for r in session.query(SeoPosition)}
    r = rows[("картинг ереван", "222:5")]
    assert r.region_name == "Ереван · Google" and r.project == "Дни рождения" and r.group_name == "Праздники"
    assert ("день рождения", "111:2") in rows  # один запрос в двух регионах не склеивается


def test_topvisor_collects_volumes_and_device_labels(session, monkeypatch):
    TV_PROJECTS[0]["searchers"][0]["regions"][1]["device"] = 2
    try:
        client, calls = _topvisor(monkeypatch, "111, 222")
        TopvisorCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20))
    finally:
        TV_PROJECTS[0]["searchers"][0]["regions"][1].pop("device")
    assert calls[0]["type_range"] == 0  # все проверки периода, а не первые 31
    assert [f for f in calls[0]["fields"] if f.startswith("volume:")] == ["volume:None:0:2"]  # Яндекс — «"запрос"»
    assert [f for f in calls[1]["fields"] if f.startswith("volume:")] == ["volume:None:1:3"]  # Google — тип 3
    vols = {(v.project, v.keyword): (v.volume_yandex, v.volume_google) for v in session.query(SeoKeyword)}
    assert vols[("Картинг", "день рождения")] == (200, None) and vols[("Дни рождения", "картинг ереван")] == (None, 100)
    names = {r.region: r.region_name for r in session.query(SeoPosition)}
    assert names["111:2"] == "Москва · Яндекс · моб." and names["111:1"] == "Ереван · Яндекс"


def test_topvisor_pages_through_keywords_and_respects_region_filter(session, monkeypatch):
    client, calls = _topvisor(monkeypatch, "111", region_index="2", page=2, keywords=["a", "b", "c", "d", "e"])
    assert TopvisorCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20)) == 5
    assert [c["offset"] for c in calls] == [0, 2, 4] and all(c["regions_indexes"] == [2] for c in calls)


def test_topvisor_unknown_project_is_an_error(session, monkeypatch):
    client, _ = _topvisor(monkeypatch, "999")
    with pytest.raises(RuntimeError, match="999"):
        TopvisorCollector(client).collect(session, date(2026, 10, 20), date(2026, 10, 20))


def test_topvisor_check_reports_missing_project_and_region(monkeypatch):
    from partyhub import check
    import partyhub.collectors.topvisor as tv

    _topvisor(monkeypatch, "111,999")
    monkeypatch.setattr(tv.TopvisorClient, "projects", lambda self: TV_PROJECTS)
    assert check._topvisor().startswith("error:проектов 999")
    _topvisor(monkeypatch, "111", region_index="5")
    assert check._topvisor().startswith("error:регионов 5")
    _topvisor(monkeypatch, "111,222")
    assert check._topvisor() == "ok:проектов 2, регионов 3 (все регионы проектов)"


def test_topvisor_retries_connect_timeouts(monkeypatch):
    import partyhub.collectors.topvisor as tv

    monkeypatch.setattr(tv.time, "sleep", lambda s: None)
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] < 3:
            raise httpx.ConnectTimeout("handshake timed out")
        return httpx.Response(200, json={"result": [{"id": 1}]})

    c = TopvisorClient("1", "k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.projects() == [{"id": 1}] and n["calls"] == 3
