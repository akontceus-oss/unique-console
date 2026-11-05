"""Вкладка «Директ»: инструменты, правки ставок, провайдеры ИИ и API."""
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from partyhub.models import Action, AdStat, CampaignSnapshot, DirectSliceStat, Lead, SearchQuery

DAY = date(2026, 10, 20)
D1 = DAY - timedelta(days=29)
AUTH = ("admin", "secret")
H = {"X-Requested-With": "partyhub"}


def ads(session, cid, cost, clicks, network="SEARCH", name=None, days=10):
    for i in range(days):
        session.add(AdStat(date=DAY - timedelta(days=i), campaign_id=cid, campaign_name=name or f"К{cid}", network=network,
                           placement="search" if network == "SEARCH" else "site.example", impressions=clicks * 10 // days,
                           clicks=clicks // days, cost=cost / days))


def leads(session, cid, n, booked=0):
    for i in range(n):
        t = datetime.combine(DAY - timedelta(days=i % 10), datetime.min.time()) + timedelta(hours=12)
        session.add(Lead(phone=f"+3749100{cid % 1000:03d}{i:02d}", first_touch_at=t, last_touch_at=t, channel="call",
                         source_label="Директ · Поиск", campaign_id=cid, status="booked" if i < booked else "in_progress"))


def snapshot(session, cid, strategy):
    session.add(CampaignSnapshot(date=DAY, campaign_id=cid, campaign_name=f"К{cid}",
                                 data={"Name": f"К{cid}", "ExcludedSites": [], "NegativeKeywords": [], "BiddingStrategy": strategy}))


def run(session, tool_id, params=None):
    from partyhub.direct import service

    return service.run_tool(session, tool_id, D1, DAY, params or {})


# ------------------------------------------------------------------ реестр ---
def test_registry_lists_tools_and_providers():
    from partyhub.direct import service

    m = service.manifest()
    ids = [t["id"] for t in m["tools"]]
    assert ids[:6] == ["placements", "search_queries", "autotargeting", "conversions", "bids", "modifiers"]
    assert "ai_review" in ids
    assert {p["id"] for p in m["providers"]} >= {"openai", "gemini", "anthropic", "yandexgpt"}
    assert all(not p["configured"] for p in m["providers"])
    assert set(m["campaign_types"]) == {"search", "network", "autotargeting", "unified"}


def test_disabled_tool_is_hidden(tmp_path, monkeypatch):
    import yaml

    from partyhub import config
    from partyhub.direct import service

    rules = config.load_rules()
    rules = {**rules, "direct_tools": {**rules["direct_tools"], "modifiers": {"enabled": False}}}
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("RULES_FILE", str(path))
    config.get_settings.cache_clear()
    config.load_rules.cache_clear()
    assert "modifiers" not in [t["id"] for t in service.manifest()["tools"]]


# ------------------------------------------------------------- инструменты ---
def test_bids_lowers_cpa_within_step(session):
    ads(session, 1, cost=200_000, clicks=500)
    leads(session, 1, n=20, booked=5)  # бронь 40 000 при цели 18 000 → снизить, но не больше чем на 20%
    snapshot(session, 1, {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 6_000_000_000}}})
    ads(session, 2, cost=50_000, clicks=200)
    leads(session, 2, n=1)
    session.flush()
    res = run(session, "bids")
    rows = {r["campaign_id"]: r for r in res["rows"]}
    assert rows[1]["status"][0] == "Снизить" and rows[1]["new_cpa"] == 4800 and rows[1]["change"] == -0.2
    assert rows[2]["status"][0] == "Мало данных"
    assert res["proposals"] == 1


def test_modifiers_need_conversions_and_stay_in_range(session):
    for i in range(10):
        d = DAY - timedelta(days=i)
        session.add(DirectSliceStat(date=d, campaign_id=5, campaign_name="К5", slice="device", value="DESKTOP", clicks=20, cost=4000, conversions=2))
        session.add(DirectSliceStat(date=d, campaign_id=5, campaign_name="К5", slice="device", value="MOBILE", clicks=40, cost=8000, conversions=1))
        session.add(DirectSliceStat(date=d, campaign_id=5, campaign_name="К5", slice="device", value="TABLET", clicks=1, cost=50, conversions=0))
    session.flush()
    res = run(session, "modifiers")
    rows = {r["group"]: r for r in res["rows"]}
    assert rows["Компьютеры"]["modifier"] == "+30%"  # выгоднее в 2 раза, но шаг ограничен ±30%
    assert rows["Смартфоны"]["modifier"] == "-30%"
    assert rows["Планшеты"]["status"][0] == "Мало данных"


def test_autotargeting_proposes_costly_queries_without_conversions(session):
    for i in range(5):
        d = DAY - timedelta(days=i)
        session.add(SearchQuery(date=d, campaign_id=7, campaign_name="К7", query="картинг москва", clicks=3, cost=3000,
                                criterion_type="AUTOTARGETING", targeting_category="BROADER", conversions=0))
        session.add(SearchQuery(date=d, campaign_id=7, campaign_name="К7", query="день рождения в картинге", clicks=5, cost=2000,
                                criterion_type="KEYWORD", criterion="день рождения в картинге", conversions=1))
    session.flush()
    res = run(session, "autotargeting")
    assert [r["query"] for r in res["rows"]] == ["картинг москва"]
    assert res["rows"][0]["status"][0] == "В минус" and res["proposals"] == 1
    assert res["summary"][0]["value"] == pytest.approx(15000 / 25000)


def test_query_words_never_minus_own_keywords(session):
    for i in range(5):
        d = DAY - timedelta(days=i)
        session.add(SearchQuery(date=d, campaign_id=7, campaign_name="К7", query="картинг аренда трассы", clicks=4, cost=3000,
                                criterion_type="KEYWORD", criterion="картинг аренда", conversions=0))
        session.add(SearchQuery(date=d, campaign_id=7, campaign_name="К7", query="картинг шлем купить", clicks=4, cost=3000,
                                criterion_type="KEYWORD", criterion="картинг", conversions=0))
    session.flush()
    res = run(session, "search_queries")
    flagged = {r["word"] for r in res["rows"] if r["status"][0] == "Дорого без конверсий"}
    assert flagged == {"шлем", "купить", "трассы"}  # «картинг» и «аренда» есть в ключевых фразах


def test_conversions_signal_campaign_without_leads(session):
    ads(session, 9, cost=40_000, clicks=300, name="Без заявок")
    session.flush()
    res = run(session, "conversions")
    assert res["rows"][0]["status"][0] == "Нет заявок"
    from partyhub.direct import service

    out = service.propose(session, "conversions", D1, DAY)
    a = session.get(Action, out["ids"][0])
    assert a.kind == "alert" and a.status == "alert"
    assert service.propose(session, "conversions", D1, DAY)["saved"] == 0  # повтор на той же неделе не создаётся


def test_propose_puts_everything_on_approval(session):
    ads(session, 1, cost=200_000, clicks=500)
    leads(session, 1, n=20, booked=5)
    snapshot(session, 1, {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 6_000_000_000}}})
    session.flush()
    from partyhub.direct import service

    out = service.propose(session, "bids", D1, DAY)
    a = session.get(Action, out["ids"][0])
    assert (a.kind, a.level, a.status) == ("set_cpa", "approval", "proposed")
    assert a.metrics["new_cpa"] == {"Search": 4800.0} and a.metrics["old_cpa"] == {"Search": 6000.0}


# ------------------------------------------------------------ правки ставок ---
class FakeDirect:
    def __init__(self):
        self.mods = []
        self.next_id = 100
        self.strategy = {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 6_000_000_000, "GoalId": 1}},
                         "Network": {"BiddingStrategyType": "SERVING_OFF"}}
        self.updates = []

    def get_campaign(self, cid):
        return {"Id": cid, "ExcludedSites": None, "NegativeKeywords": None, "TextCampaign": {"BiddingStrategy": json.loads(json.dumps(self.strategy))}}

    def update_campaign(self, c):
        self.updates.append(c)
        for side, s in c["TextCampaign"]["BiddingStrategy"].items():
            self.strategy[side] = s

    def get_bid_modifiers(self, cid):
        return [dict(m) for m in self.mods]

    def add_bid_modifier(self, cid, adj):
        self.next_id += 1
        if "DemographicsAdjustments" in adj:
            self.mods.append({"Id": self.next_id, "DemographicsAdjustment": adj["DemographicsAdjustments"][0]})
        else:
            self.mods.append({"Id": self.next_id, **adj})

    def set_bid_modifier(self, mid, value):
        for m in self.mods:
            if m["Id"] == mid:
                for v in m.values():
                    if isinstance(v, dict):
                        v["BidModifier"] = value

    def delete_bid_modifier(self, mid):
        self.mods = [m for m in self.mods if m["Id"] != mid]


def action(session, kind, metrics, target="x"):
    a = Action(created_at=datetime(2026, 10, 21, 7), rule="direct", level="approval", kind=kind, campaign_ids=[1], campaign_names=["К1"],
               target=target, reason="тест", metrics=metrics, status="proposed")
    session.add(a)
    session.flush()
    return a


def test_bid_modifier_apply_and_rollback(session):
    from partyhub.rules.actions import ActionApplier

    fake = FakeDirect()
    applier = ActionApplier(session, mode="sandbox", client=fake)
    a = action(session, "bid_modifier", {"slice": "demography", "value": "GENDER_FEMALE|AGE_25_34", "modifier": 120})
    applier.approve(a, by="тест")
    assert a.status == "applied", a.result
    assert fake.mods[0]["DemographicsAdjustment"] == {"Gender": "GENDER_FEMALE", "Age": "AGE_25_34", "BidModifier": 120}
    assert a.metrics["previous"] == {"1": None}
    applier.rollback(a, by="тест")
    assert fake.mods == [] and a.status == "rolled_back"


def test_bid_modifier_rollback_restores_previous_value(session):
    from partyhub.rules.actions import ActionApplier

    fake = FakeDirect()
    fake.mods = [{"Id": 7, "MobileAdjustment": {"BidModifier": 90}}]
    applier = ActionApplier(session, mode="sandbox", client=fake)
    a = action(session, "bid_modifier", {"slice": "device", "value": "MOBILE", "modifier": 70})
    applier.approve(a, by="тест")
    assert fake.mods[0]["MobileAdjustment"]["BidModifier"] == 70
    applier.rollback(a, by="тест")
    assert fake.mods[0]["MobileAdjustment"]["BidModifier"] == 90


def test_bid_modifier_out_of_range_fails(session):
    from partyhub.rules.actions import ActionApplier

    a = action(session, "bid_modifier", {"slice": "device", "value": "MOBILE", "modifier": 300})
    ActionApplier(session, mode="sandbox", client=FakeDirect()).approve(a, by="тест")
    assert a.status == "failed" and "диапазона" in a.result


def test_set_cpa_writes_micros_and_rolls_back(session):
    from partyhub.rules.actions import ActionApplier

    fake = FakeDirect()
    applier = ActionApplier(session, mode="sandbox", client=fake)
    a = action(session, "set_cpa", {"new_cpa": {"Search": 5200}, "old_cpa": {"Search": 6000}})
    applier.approve(a, by="тест")
    assert a.status == "applied", a.result
    sent = fake.updates[0]["TextCampaign"]["BiddingStrategy"]
    assert sent == {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 5_200_000_000, "GoalId": 1}}}
    applier.rollback(a, by="тест")
    assert fake.strategy["Search"]["AverageCpa"]["AverageCpa"] == 6_000_000_000


def test_set_cpa_refuses_if_changed_since_proposal(session):
    from partyhub.rules.actions import ActionApplier

    fake = FakeDirect()
    fake.strategy["Search"]["AverageCpa"]["AverageCpa"] = 9_000_000_000  # кто-то поменял в кабинете
    a = action(session, "set_cpa", {"new_cpa": {"Search": 5200}, "old_cpa": {"Search": 6000}})
    ActionApplier(session, mode="sandbox", client=fake).approve(a, by="тест")
    assert a.status == "failed" and "пересчитайте" in a.result and not fake.updates


def test_dry_run_describes_new_kinds(session):
    from partyhub.rules.actions import ActionApplier

    a = action(session, "bid_modifier", {"slice": "device", "value": "MOBILE", "modifier": 80})
    ActionApplier(session, mode="dry_run").approve(a, by="тест")
    assert a.status == "applied" and "Смартфоны" in a.result and "не отправлялось" in a.result


# ---------------------------------------------------------------- провайдеры ---
def provider(name, monkeypatch, handler, **env):
    from partyhub import config
    from partyhub.ai.providers import get_provider

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    return get_provider(name, httpx.Client(transport=httpx.MockTransport(handler)))


def test_openai_request_and_json(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["auth"] = str(req.url), json.loads(req.content), req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"ok": 1}\n```'}}]})

    p = provider("openai", monkeypatch, handler, OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://llm.example/v1/")
    assert p.complete_json("sys", "user") == {"ok": 1}
    assert seen["url"] == "https://llm.example/v1/chat/completions" and seen["auth"] == "Bearer sk-test"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["messages"][0]["role"] == "system"


def test_gemini_request(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers["x-goog-api-key"], json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "думаю", "thought": True}, {"text": "Ответ"}]}}]})

    p = provider("gemini", monkeypatch, handler, GEMINI_API_KEY="g-key", GEMINI_MODEL="gemini-test")
    assert p.complete("sys", "user") == "Ответ"
    assert seen["url"].endswith("/models/gemini-test:generateContent") and seen["key"] == "g-key"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"


def test_provider_errors_are_readable(monkeypatch):
    from partyhub.ai.providers import LLMError

    p = provider("anthropic", monkeypatch, lambda req: httpx.Response(429, text="rate limited"), ANTHROPIC_API_KEY="a-key")
    with pytest.raises(LLMError, match="HTTP 429"):
        p.complete("sys", "user")
    p = provider("yandexgpt", monkeypatch, lambda req: httpx.Response(200, json={}))
    with pytest.raises(LLMError, match="не заданы ключи"):
        p.complete("sys", "user")


def test_report_notes_use_provider(monkeypatch):
    from partyhub.ai import notes

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "— наблюдение"}}]})

    monkeypatch.setenv("AI_PROVIDER", "openai")
    provider("openai", monkeypatch, handler, OPENAI_API_KEY="sk-test")
    assert notes.generate_notes({"x": 1}, http=httpx.Client(transport=httpx.MockTransport(handler))) == "— наблюдение"


# ------------------------------------------------------------------- ИИ-аудит ---
class FakeLLM:
    id, title, model = "fake", "Фейк", "fake-1"

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def configured(self):
        return True

    def complete_json(self, system, user, max_tokens=0):
        self.calls += 1
        self.facts = json.loads(user.split("\n", 1)[1])
        return self.answer


def test_ai_review_validates_recommendations(session, monkeypatch):
    ads(session, 1, cost=200_000, clicks=500)
    leads(session, 1, n=20, booked=5)
    ads(session, 3, cost=30_000, clicks=200, network="AD_NETWORK", name="РСЯ")
    snapshot(session, 1, {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 6_000_000_000}}})
    session.add(DirectSliceStat(date=DAY, campaign_id=1, campaign_name="К1", slice="device", value="MOBILE", clicks=10, cost=100, conversions=1))
    session.flush()
    llm = FakeLLM({"summary": "Бронь дорогая.", "recommendations": [
        {"type": "exclude_site", "campaign_id": 3, "target": "site.example", "reason": "дорого", "priority": "high"},
        {"type": "exclude_site", "campaign_id": 3, "target": "выдуманная.площадка", "reason": "?"},
        {"type": "set_cpa", "campaign_id": 1, "value": 5000, "reason": "дорого"},
        {"type": "set_cpa", "campaign_id": 1, "value": 2000, "reason": "слишком резко"},
        {"type": "bid_modifier", "campaign_id": 1, "target": "MOBILE", "value": 80, "reason": "дорого"},
        {"type": "bid_modifier", "campaign_id": 999, "target": "MOBILE", "value": 80, "reason": "нет кампании"},
        {"type": "note", "campaign_id": None, "reason": "переписать объявления"},
    ]})
    import partyhub.direct.tools.ai_review as ai

    monkeypatch.setattr(ai, "get_provider", lambda name=None: llm)
    res = run(session, "ai_review", {"provider": "fake"})
    checks = [r["check"][0] for r in res["rows"]]
    assert checks == ["Можно в журнал", "Нет такой площадки", "Можно в журнал", "Шаг больше 20%", "Можно в журнал",
                      "Нет такой кампании", "Идея"]
    assert res["proposals"] == 3 and "Бронь дорогая" in res["notes"][0]
    assert "phone" not in json.dumps(llm.facts, ensure_ascii=False)

    from partyhub.direct import service

    out = service.propose(session, "ai_review", D1, DAY, {"provider": "fake"})
    assert out["saved"] == 3 and llm.calls == 1  # в журнал — то, что человек видел, без повторного запроса
    kinds = {a.kind: a for a in session.query(Action).filter(Action.rule == "ai_review")}
    assert set(kinds) == {"exclude_site", "set_cpa", "bid_modifier"} and all(a.level == "approval" for a in kinds.values())
    assert kinds["set_cpa"].metrics["new_cpa"] == {"Search": 5000.0}


def test_ai_propose_without_run_is_refused(session):
    from partyhub.direct import service

    with pytest.raises(ValueError):
        service.propose(session, "ai_review", D1, DAY, {"provider": "openai"})


# ----------------------------------------------------------------------- API ---
@pytest.fixture
def client(fixed_now):
    from partyhub.web.app import app

    with TestClient(app) as c:
        yield c


def test_api_direct(client):
    assert client.get("/api/direct/manifest").status_code == 401
    m = client.get("/api/direct/manifest", auth=AUTH).json()
    assert m["mode"] == "dry_run" and m["currency"] == "֏"
    r = client.get("/api/direct/tools/conversions?days=30", auth=AUTH)
    assert r.status_code == 200 and r.json()["notes"]
    assert client.get("/api/direct/tools/nope", auth=AUTH).status_code == 404
    assert client.post("/api/direct/tools/bids/propose", auth=AUTH).status_code == 403
    assert client.post("/api/direct/tools/bids/propose", auth=AUTH, headers=H).json()["saved"] == 0
    r = client.post("/api/direct/ask", auth=AUTH, headers=H, json={"question": "что делать?", "provider": "gemini"})
    assert r.status_code == 502 and "не настроен" in r.json()["detail"]


def test_dashboard_has_direct_tab(client):
    from partyhub.projects import create_project

    create_project("Проверка", "check")
    html = client.get("/p/check/", auth=AUTH).text
    assert 'data-tab="direct"' in html and 'id="dx-tools"' in html and "<!--DIRECT-->" not in html


def test_daily_runs_daily_tools(session, fixed_now):
    ads(session, 9, cost=40_000, clicks=300, name="Без заявок")
    session.commit()
    from partyhub.direct.service import daily_tools

    out = daily_tools(session, DAY)
    assert out["conversions"] == 1
    assert set(out) == {"search_queries", "autotargeting", "conversions", "bids", "modifiers", "ai_review"}
    assert out["ai_review"] == "пропущено: нейросеть не подключена"


def test_new_columns_added_to_old_table(tmp_path):
    import sqlalchemy as sa

    from partyhub.db import create_all, init_engine

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE search_queries (id INTEGER PRIMARY KEY, date DATE, campaign_id BIGINT, campaign_name VARCHAR(255),"
                          " ad_group_id BIGINT, query VARCHAR(500), impressions INTEGER, clicks INTEGER, cost NUMERIC(14,2))"))
    init_engine(url)
    create_all()
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("search_queries")}
    assert {"criterion_type", "criterion", "targeting_category", "conversions"} <= cols


def test_collector_saves_slices_and_falls_back_without_extra_query_fields(session, monkeypatch):
    from partyhub import config
    from partyhub.collectors.direct import DirectClient, DirectCollector

    monkeypatch.setenv("DIRECT_GOAL_IDS", "111")
    config.get_settings.cache_clear()
    seen = []

    def handler(request):
        if request.url.path.endswith("/reports"):
            p = json.loads(request.content)["params"]
            seen.append(p["FieldNames"])
            if p["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT":
                if "TargetingCategory" in p["FieldNames"]:
                    return httpx.Response(400, json={"error": {"error_code": 4000, "error_string": "Неверное поле"}})
                return httpx.Response(200, text="Date\tCampaignId\tCampaignName\tAdGroupId\tQuery\tImpressions\tClicks\tCost\n"
                                                 "2026-10-20\t1\tК1\t11\tкартинг\t10\t2\t800\n")
            if "Device" in p["FieldNames"]:
                assert p["Goals"] == [111]
                return httpx.Response(200, text="Date\tCampaignId\tCampaignName\tImpressions\tClicks\tCost\tDevice\tConversions_111_AUTO\n"
                                                 "2026-10-20\t1\tК1\t100\t5\t2000\tMOBILE\t2\n")
            if "Gender" in p["FieldNames"]:
                return httpx.Response(200, text="Date\tCampaignId\tCampaignName\tImpressions\tClicks\tCost\tGender\tAge\tConversions_111_AUTO\n"
                                                 "2026-10-20\t1\tК1\t100\t5\t2000\tGENDER_FEMALE\tAGE_25_34\t1\n")
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"result": {"Campaigns": []}})

    client = DirectClient("T", http=httpx.Client(transport=httpx.MockTransport(handler)))
    col = DirectCollector(client)
    col.collect(session, DAY, DAY)
    assert col.warning == ""
    q = session.query(SearchQuery).one()
    assert q.query == "картинг" and q.criterion_type is None
    s = {(x.slice, x.value): x.conversions for x in session.query(DirectSliceStat)}
    assert s == {("device", "MOBILE"): 2, ("demography", "GENDER_FEMALE|AGE_25_34"): 1}
