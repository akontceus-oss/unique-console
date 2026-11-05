"""ИИ-агент Директа: диалог с вызовом функций, план изменений, применение и откат."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from partyhub.direct import agent
from partyhub.direct.api import DirectAPI, micros
from partyhub.direct.changeset import OPS, limits
from partyhub.models import DirectAgentRun, DirectChangeset

AUTH = ("admin", "secret")
H = {"X-Requested-With": "partyhub"}

AD = {"title": "Аренда зала для праздника", "text": "Зал на 40 гостей в центре. Звоните!", "href": "https://example.com/?utm_source=yandex"}
CAMPAIGN = {"name": "Поиск — дни рождения", "daily_budget": 3000, "networks": "search", "strategy": {"type": "manual"},
            "negative_keywords": ["бесплатно"], "sitelinks": [{"title": "Цены", "href": "https://example.com/price"}],
            "groups": [{"name": "Дни рождения", "region_ids": [10262],
                        "keywords": [{"phrase": "зал для дня рождения", "bid": 12.5}], "ads": [AD]}]}


class FakeClient:
    """Вместо DirectClient: запоминает вызовы и выдаёт новые id."""

    def __init__(self, fail_on: str | None = None):
        self.calls = []
        self.next_id = 100
        self.fail_on = fail_on
        self.campaign = {"Id": 7, "Name": "Старая", "DailyBudget": {"Amount": micros(1000), "Mode": "STANDARD"},
                         "NegativeKeywords": {"Items": ["даром"]}, "ExcludedSites": None}

    def call(self, service, method, params):
        self.calls.append((service, method, params))
        if f"{service}.{method}" == self.fail_on:
            return {"AddResults": [{"Errors": [{"Code": 8800, "Message": "Объект не найден"}]}]}
        if method == "get" and service == "campaigns":
            return {"Campaigns": [self.campaign]}
        if method == "get" and service == "dictionaries":
            return {"GeoRegions": [{"GeoRegionId": 10262, "GeoRegionName": "Ереван", "GeoRegionType": "City"},
                                   {"GeoRegionId": 213, "GeoRegionName": "Москва", "GeoRegionType": "City"}]}
        if method == "get":
            return {}
        key = {"add": "AddResults", "update": "UpdateResults", "set": "SetResults"}.get(method, method.capitalize() + "Results")
        n = len(next(iter(params.values()))) if method in ("add", "update", "set") else len(params["SelectionCriteria"]["Ids"])
        out = []
        for _ in range(n):
            self.next_id += 1
            out.append({"Id": self.next_id})
        return {key: out}

    def methods(self):
        return [f"{s}.{m}" for s, m, _ in self.calls]


class FakeLLM:
    """Модель по сценарию: отдаёт заранее заданные ответы и запоминает, что ей прислали."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, system, messages, tools, max_tokens=4000):
        self.seen.append([dict(m) for m in messages])
        out = self.script.pop(0)
        return {"content": out.get("content", ""), "tool_calls": [{"id": f"c{i}", **c} for i, c in enumerate(out.get("tool_calls", []))]}


def plan_with(session, *ops):
    run = DirectAgentRun(created_at=agent.now_local(), updated_at=agent.now_local(), task="t", provider="x", status="done",
                         messages=[], steps=[])
    session.add(run)
    session.flush()
    box = agent.Toolbox(session, run)
    for name, params in ops:
        assert box.propose(name, params)["ok"], box.propose(name, params)
    session.commit()
    return run.changeset_id


# ---------------------------------------------------------------- проверки ---
def test_create_campaign_validation():
    lim = limits()
    assert OPS["create_campaign"].validate(CAMPAIGN, lim) == []
    bad = {**CAMPAIGN, "daily_budget": 10**6,
           "groups": [{"name": "Г", "region_ids": [], "keywords": [], "ads": [{**AD, "title": "x" * 60}]}]}
    errs = " | ".join(OPS["create_campaign"].validate(bad, lim))
    assert "выше лимита" in errs and "регионы" in errs and "ключевые фразы" in errs and "56" in errs


def test_protected_campaign_cannot_be_changed(tmp_path, monkeypatch):
    rules = tmp_path / "rules.yaml"
    from partyhub import config

    rules.write_text("direct_agent:\n  protected_campaign_ids: [7]\n", encoding="utf-8")
    monkeypatch.setenv("RULES_FILE", str(rules))
    config.get_settings.cache_clear()
    config.load_rules.cache_clear()
    errs = OPS["set_state"].validate({"target": "campaign", "ids": [7], "action": "suspend"}, limits())
    assert errs and "7" in errs[0]


def test_micros_keeps_kopecks():
    assert micros(12.5) == 12_500_000 and micros(3000) == 3_000_000_000


# ------------------------------------------------------------------ агент ---
def test_agent_loop_builds_plan(session):
    llm = FakeLLM([
        {"tool_calls": [{"name": "list_campaigns", "args": {}}, {"name": "find_regions", "args": {"query": "ереван"}}]},
        {"content": "Собираю кампанию", "tool_calls": [{"name": "propose_create_campaign", "args": CAMPAIGN}]},
        {"content": "План готов: одна кампания черновиком."},
    ])
    api = DirectAPI(FakeClient())
    run_id = agent.start("Создай поисковую кампанию на дни рождения", "gemini", "admin", sync=True, llm=llm, api=api)
    run = session.get(DirectAgentRun, run_id)
    assert run.status == "done" and run.answer.startswith("План готов")
    view = agent.run_view(session, run)
    assert [s["type"] for s in view["steps"]] == ["task", "call", "call", "thought", "call", "answer"]
    assert view["changeset"]["status"] == "draft" and len(view["changeset"]["ops"]) == 1
    assert "черновиком" in view["changeset"]["ops"][0]["description"]
    # модели вернулись результаты обоих вызовов, регион нашёлся
    tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and "10262" in tool_msgs[1]["content"]
    # чтение — да, запись — нет: план ещё не применён
    assert not [m for m in api.c.methods() if not m.endswith(".get")]


def test_agent_gets_validation_errors_and_reply_continues(session):
    llm = FakeLLM([
        {"tool_calls": [{"name": "propose_create_campaign", "args": {**CAMPAIGN, "daily_budget": 999999}}]},
        {"content": "Бюджет выше лимита — уточните."},
        {"tool_calls": [{"name": "propose_update_campaign", "args": {"campaign_id": 7, "daily_budget": 2000}}]},
        {"content": "Готово"},
    ])
    run_id = agent.start("Кампания на миллион", "openai", sync=True, llm=llm, api=DirectAPI(FakeClient()))
    run = session.get(DirectAgentRun, run_id)
    assert run.changeset_id is None and "выше лимита" in json.dumps(run.steps, ensure_ascii=False)
    agent.reply(run_id, "Тогда просто подними бюджет кампании 7 до 2000", sync=True, llm=llm, api=DirectAPI(FakeClient()))
    session.expire_all()
    run = session.get(DirectAgentRun, run_id)
    assert run.answer == "Готово" and len(session.get(DirectChangeset, run.changeset_id).ops) == 1
    assert [m["role"] for m in run.messages].count("user") == 2


def test_agent_stops_after_max_steps(session):
    llm = FakeLLM([{"tool_calls": [{"name": "list_campaigns", "args": {}}]}] * 30)
    run_id = agent.start("Зациклись", "openai", sync=True, llm=llm, api=DirectAPI(FakeClient()))
    run = session.get(DirectAgentRun, run_id)
    assert run.status == "done" and "слишком много шагов" in run.answer and len(llm.seen) == limits()["max_steps"]


def test_unknown_tool_and_bad_args_go_back_to_model(session):
    run = DirectAgentRun(created_at=agent.now_local(), updated_at=agent.now_local(), task="t", provider="x", status="running",
                         messages=[], steps=[])
    session.add(run)
    session.flush()
    box = agent.Toolbox(session, run, DirectAPI(FakeClient()))
    assert "нет функции" in box.call("drop_everything", {})["error"]
    assert "неверные параметры" in box.call("get_campaign", {"wrong": 1})["error"]
    assert "нет операции" in box.call("propose_nuke", {})["error"]


def _metrika_box(session, monkeypatch, handler):
    from partyhub import config
    from partyhub.collectors import metrika

    monkeypatch.setenv("METRIKA_TOKEN", "M")
    monkeypatch.setenv("METRIKA_COUNTER_ID", "123")
    monkeypatch.setenv("METRIKA_GOAL_ID", "11")
    monkeypatch.setenv("METRIKA_GOAL_IDS", "11,22")
    monkeypatch.setenv("DIRECT_GOAL_IDS", "11")
    config.get_settings.cache_clear()
    real = metrika.MetrikaClient

    def fake(token, counter):
        return real(token, counter, http=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(metrika, "MetrikaClient", fake)
    run = DirectAgentRun(created_at=agent.now_local(), updated_at=agent.now_local(), task="t", provider="x", status="running",
                         messages=[], steps=[])
    session.add(run)
    session.flush()
    return agent.Toolbox(session, run, DirectAPI(FakeClient()))


def test_agent_reads_metrika_report_and_goals(session, monkeypatch):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/goals"):
            return httpx.Response(200, json={"goals": [{"id": 11, "name": "Заявка"}, {"id": 22, "name": "WhatsApp"}, {"id": 33, "name": "Скролл"}]})
        seen.update(request.url.params)
        return httpx.Response(200, json={"total_rows": 1, "data": [{"dimensions": [{"name": "Поиск — ДР"}], "metrics": [120, 31.5, 4]}]})

    box = _metrika_box(session, monkeypatch, handler)
    res = box.call("metrika_report", {"date_from": "2026-10-01", "date_to": "2026-10-30", "dimensions": ["ym:s:lastDirectClickOrder"],
                                      "metrics": ["ym:s:visits", "ym:s:bounceRate", "ym:s:goal22reaches"], "filters": "ym:s:UTMSource=='yandex'"})
    assert res == {"rows": [{"ym:s:lastDirectClickOrder": "Поиск — ДР", "ym:s:visits": 120, "ym:s:bounceRate": 31.5, "ym:s:goal22reaches": 4}],
                   "total_rows": 1}
    assert seen["ids"] == "123" and seen["filters"] == "ym:s:UTMSource=='yandex'"
    goals = {g["id"]: g for g in box.call("metrika_goals", {})["goals"]}
    assert goals["11"]["main"] and goals["11"]["direct_conversion"] and goals["22"]["in_console"]
    assert not goals["33"]["in_console"] and not goals["22"]["direct_conversion"]


def test_agent_cannot_read_visitor_ids_from_metrika(session, monkeypatch):
    box = _metrika_box(session, monkeypatch, lambda r: httpx.Response(500))
    res = box.call("metrika_report", {"date_from": "2026-10-01", "date_to": "2026-10-30", "dimensions": ["ym:s:clientID"], "metrics": ["ym:s:visits"]})
    assert "недоступны" in res["error"]


def test_agent_metrika_not_connected(session, monkeypatch):
    from partyhub import config

    monkeypatch.setenv("METRIKA_TOKEN", "")
    config.get_settings.cache_clear()
    run = DirectAgentRun(created_at=agent.now_local(), updated_at=agent.now_local(), task="t", provider="x", status="running",
                         messages=[], steps=[])
    session.add(run)
    session.flush()
    res = agent.Toolbox(session, run, DirectAPI(FakeClient())).call("metrika_goals", {})
    assert "Метрика не подключена" in res["error"]


# ------------------------------------------------------- применение плана ---
def test_apply_dry_run_changes_nothing(session):
    cs_id = plan_with(session, ("create_campaign", CAMPAIGN))
    api = DirectAPI(FakeClient())
    cs = agent.apply_changeset(session, cs_id, "admin", api=api, mode="dry_run")
    assert cs.status == "applied" and cs.ops[0]["result"] == {"dry_run": True} and api.c.calls == []
    with pytest.raises(agent.PlanError):
        agent.apply_changeset(session, cs_id, "admin", api=api, mode="dry_run")


def test_apply_and_rollback_in_sandbox(session, monkeypatch):
    monkeypatch.setenv("DIRECT_MODE", "sandbox")
    from partyhub import config

    config.get_settings.cache_clear()
    cs_id = plan_with(session, ("create_campaign", CAMPAIGN),
                      ("update_campaign", {"campaign_id": 7, "daily_budget": 2000, "negative_keywords_add": ["скачать"]}))
    fake = FakeClient()
    cs = agent.apply_changeset(session, cs_id, "admin", api=DirectAPI(fake), mode="sandbox")
    assert cs.status == "applied" and cs.mode == "sandbox"
    m = fake.methods()
    assert m[:5] == ["sitelinks.add", "campaigns.add", "adgroups.add", "keywords.add", "ads.add"]
    assert "ads.moderate" not in m  # черновик: на модерацию не отправляли
    camp = fake.calls[1][2]["Campaigns"][0]
    assert camp["UnifiedCampaign"]["BiddingStrategy"]["Search"]["BiddingStrategyType"] == "HIGHEST_POSITION"
    assert camp["DailyBudget"]["Amount"] == 3_000_000_000
    assert fake.calls[3][2]["Keywords"][0]["Bid"] == 12_500_000
    upd = [p for s, meth, p in fake.calls if s == "campaigns" and meth == "update"][0]["Campaigns"][0]
    assert upd["NegativeKeywords"] == {"Items": ["даром", "скачать"]} and upd["DailyBudget"]["Amount"] == 2_000_000_000

    fake.calls.clear()
    cs = agent.rollback_changeset(session, cs_id, "admin", api=DirectAPI(fake))
    assert cs.status == "rolled_back"
    restored = fake.calls[0][2]["Campaigns"][0]
    assert restored["DailyBudget"]["Amount"] == micros(1000) and restored["NegativeKeywords"] == {"Items": ["даром"]}
    assert fake.methods()[1] == "campaigns.delete"


def test_partial_apply_stops_on_error(session):
    cs_id = plan_with(session, ("update_campaign", {"campaign_id": 7, "name": "Новая"}),
                      ("create_campaign", CAMPAIGN), ("set_state", {"target": "campaign", "ids": [7], "action": "suspend"}))
    fake = FakeClient(fail_on="campaigns.add")
    cs = agent.apply_changeset(session, cs_id, "admin", api=DirectAPI(fake), mode="sandbox")
    assert cs.status == "partial"
    assert "result" in cs.ops[0] and "8800" in cs.ops[1]["error"] and "result" not in cs.ops[2]
    assert "campaigns.suspend" not in fake.methods()


def test_reject(session):
    cs_id = plan_with(session, ("set_state", {"target": "ad", "ids": [1, 2], "action": "suspend"}))
    assert agent.reject_changeset(session, cs_id, "admin").status == "rejected"
    with pytest.raises(agent.PlanError):
        agent.apply_changeset(session, cs_id, "admin", mode="dry_run")


# --------------------------------------------------- форматы провайдеров ---
HISTORY = [
    {"role": "user", "content": "Покажи кампании"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "list_campaigns", "args": {}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "list_campaigns", "content": json.dumps({"campaigns": []})},
]
TOOLS = [{"name": "list_campaigns", "description": "Список", "parameters": {"type": "object", "properties": {}}}]


def _provider(name, reply, monkeypatch, key_env):
    monkeypatch.setenv(key_env, "k")
    from partyhub import config
    from partyhub.ai.providers import get_provider

    config.get_settings.cache_clear()
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=reply)

    return get_provider(name, http=httpx.Client(transport=httpx.MockTransport(handler))), sent


def test_openai_tool_calls(monkeypatch):
    p, sent = _provider("openai", {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "x1", "type": "function", "function": {"name": "find_regions", "arguments": '{"query": "Ереван"}'}}]}}]},
        monkeypatch, "OPENAI_API_KEY")
    out = p.chat("sys", HISTORY, TOOLS)
    assert out["tool_calls"] == [{"id": "x1", "name": "find_regions", "args": {"query": "Ереван"}}]
    msgs = sent[0]["messages"]
    assert msgs[0]["role"] == "system" and msgs[2]["tool_calls"][0]["function"]["name"] == "list_campaigns"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"campaigns": []}'}
    assert sent[0]["tools"][0]["function"]["name"] == "list_campaigns"


def test_gemini_tool_calls_keep_signature(monkeypatch):
    parts = [{"functionCall": {"name": "find_regions", "args": {"query": "Ереван"}}, "thoughtSignature": "sig"}]
    p, sent = _provider("gemini", {"candidates": [{"content": {"parts": parts}}]}, monkeypatch, "GEMINI_API_KEY")
    out = p.chat("sys", HISTORY, TOOLS)
    assert out["tool_calls"][0]["name"] == "find_regions" and out["raw"] == {"gemini": parts}
    history = HISTORY + [{"role": "assistant", "content": "", "tool_calls": out["tool_calls"], "raw": out["raw"]}]
    p.chat("sys", history, TOOLS)
    contents = sent[1]["contents"]
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"result": {"campaigns": []}}
    assert contents[3]["parts"][0]["thoughtSignature"] == "sig"
    assert sent[0]["tools"] == [{"functionDeclarations": TOOLS}]


def test_anthropic_tool_calls(monkeypatch):
    p, sent = _provider("anthropic", {"content": [{"type": "text", "text": "Смотрю"},
                                                  {"type": "tool_use", "id": "t1", "name": "find_regions", "input": {"query": "Ереван"}}]},
                        monkeypatch, "ANTHROPIC_API_KEY")
    out = p.chat("sys", HISTORY, TOOLS)
    assert out["content"] == "Смотрю" and out["tool_calls"] == [{"id": "t1", "name": "find_regions", "args": {"query": "Ереван"}}]
    msgs = sent[0]["messages"]
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"campaigns": []}'}]}
    assert sent[0]["tools"][0]["input_schema"] == TOOLS[0]["parameters"]


def test_json_fallback_for_models_without_tools(monkeypatch):
    reply = {"result": {"alternatives": [{"message": {"text": '{"tool_calls": [{"name": "list_campaigns", "args": {}}]}'}}]}}
    monkeypatch.setenv("YANDEXGPT_FOLDER_ID", "f")
    p, sent = _provider("yandexgpt", reply, monkeypatch, "YANDEXGPT_API_KEY")
    out = p.chat("sys", HISTORY[:1], TOOLS)
    assert out["tool_calls"][0]["name"] == "list_campaigns" and out["tool_calls"][0]["id"]
    assert "list_campaigns" in json.dumps(sent[0], ensure_ascii=False)


# ------------------------------------------------------------------- веб ---
@pytest.fixture
def client(fixed_now):
    from partyhub.web.app import app

    with TestClient(app) as c:
        yield c


def test_agent_endpoints(client, session, monkeypatch):
    llm = FakeLLM([{"tool_calls": [{"name": "propose_set_state", "args": {"target": "ad", "ids": [5], "action": "suspend"}}]},
                   {"content": "Остановлю объявление 5"}])
    real_start = agent.start
    monkeypatch.setattr(agent, "start", lambda task, provider, author="": real_start(task, provider, author, sync=True, llm=llm,
                                                                                     api=DirectAPI(FakeClient())))
    assert client.post("/api/direct/agent", auth=AUTH, json={"task": "x"}).status_code == 403
    r = client.post("/api/direct/agent", auth=AUTH, headers=H, json={"task": "Останови объявление 5", "provider": "openai"})
    assert r.status_code == 200
    run_id = r.json()["id"]
    view = client.get(f"/api/direct/agent/{run_id}", auth=AUTH).json()
    assert view["status"] == "done" and view["changeset"]["ops"][0]["description"].startswith("Остановить объявления")
    assert client.get("/api/direct/agent", auth=AUTH).json()["runs"][0]["id"] == run_id
    cs_id = view["changeset"]["id"]
    assert client.post(f"/api/direct/changesets/{cs_id}/apply", auth=AUTH).status_code == 403
    r = client.post(f"/api/direct/changesets/{cs_id}/apply", auth=AUTH, headers=H)
    assert r.json()["status"] == "applied" and r.json()["mode"] == "dry_run"
    assert client.post(f"/api/direct/changesets/{cs_id}/apply", auth=AUTH, headers=H).status_code == 409
    assert client.post(f"/api/direct/changesets/{cs_id}/rollback", auth=AUTH, headers=H).json()["status"] == "rolled_back"
    assert client.post(f"/api/direct/changesets/{cs_id}/nope", auth=AUTH, headers=H).status_code == 400


def test_agent_needs_configured_provider(client):
    r = client.post("/api/direct/agent", auth=AUTH, headers=H, json={"task": "x", "provider": "gemini"})
    assert r.status_code in (400, 502) and "не настроен" in r.json()["detail"]


def test_dashboard_has_agent(client):
    from partyhub.projects import create_project

    create_project("Проверка", "check")
    html = client.get("/p/check/", auth=AUTH).text
    assert 'id="dx-agent"' in html and 'id="dx-plan"' in html
