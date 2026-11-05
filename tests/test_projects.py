import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from partyhub import projects as P
from partyhub.config import get_settings, load_rules
from partyhub.db import session_scope
from partyhub.models import Touch

AUTH = ("admin", "secret")
H = {"X-Requested-With": "partyhub"}


@pytest.fixture
def client(fixed_now):
    from partyhub.web.app import app

    with TestClient(app) as c:
        yield c


def test_project_settings_overlay_and_encrypted_secrets():
    P.create_project("Crazy Drift")
    P.update_settings("crazy-drift", {"direct_token": "tok-123", "direct_client_login": "unique-marketing",
                                      "direct_mode": "sandbox", "topvisor_project_id": "111,222",
                                      "rules": {"economics": {"target_cpl": 1500}}})
    raw = P.get_project("crazy-drift")
    assert "tok-123" not in raw.secrets_enc and "tok-123" not in raw.settings_json  # ключ зашифрован
    assert get_settings().direct_token == ""  # вне проекта — общие настройки
    with P.use_project("crazy-drift"):
        s = get_settings()
        assert (s.direct_token, s.direct_client_login, s.direct_mode) == ("tok-123", "unique-marketing", "sandbox")
        assert s.business_name == "Crazy Drift" and s.dashboard_password == "secret"  # общее — из .env
        assert load_rules()["economics"]["target_cpl"] == 1500
        assert load_rules()["economics"]["target_cpb"]  # остальное — из rules.yaml
    # пустое поле ключа — «не менять», None — стереть
    P.update_settings("crazy-drift", {"direct_token": ""})
    with P.use_project("crazy-drift"):
        assert get_settings().direct_token == "tok-123"
    P.update_settings("crazy-drift", {"direct_token": None})
    with P.use_project("crazy-drift"):
        assert get_settings().direct_token == ""


def test_project_validation():
    P.create_project("Картинг Москва")
    assert P.get_project("karting-moskva")
    with pytest.raises(ValueError, match="уже есть"):
        P.create_project("Другой", "karting-moskva")
    with pytest.raises(ValueError):
        P.create_project("x", "Bad Slug!")
    with pytest.raises(ValueError, match="Неизвестные"):
        P.update_settings("karting-moskva", {"database_url": "sqlite:///hack.db"})
    with pytest.raises(ValueError, match="Режим"):
        P.update_settings("karting-moskva", {"direct_mode": "prod"})


def test_projects_have_separate_databases():
    P.create_project("A", "proj-a")
    P.create_project("B", "proj-b")
    for slug in ("proj-a", "proj-b"):
        with P.use_project(slug), session_scope() as s:
            s.add(Touch(source="form", system="site", external_id=slug, created_at=datetime(2026, 10, 20), phone="+37491000001"))
    with P.use_project("proj-a"), session_scope() as s:
        assert [t.external_id for t in s.query(Touch)] == ["proj-a"]
    with session_scope() as s:  # общая база не тронута
        assert s.query(Touch).count() == 0


def test_web_projects_list_create_and_cabinet(client):
    assert client.get("/", auth=AUTH).text.count("Новый проект") == 1  # список проектов
    assert client.post("/api/projects", auth=AUTH, json={"name": "Crazy Drift"}).status_code == 403  # без заголовка
    r = client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    assert r.json() == {"slug": "crazy-drift", "url": "/p/crazy-drift/"}
    assert client.get("/api/projects", auth=AUTH).json()["projects"][0]["slug"] == "crazy-drift"
    assert client.get("/p/crazy-drift", auth=AUTH, follow_redirects=False).status_code == 307
    html = client.get("/p/crazy-drift/", auth=AUTH).text
    assert 'data-tab="connections"' in html and 'id="cx-blocks"' in html
    assert client.get("/p/nope/", auth=AUTH).status_code == 404
    assert client.get("/p/crazy-drift/api/datasets?days=7", auth=AUTH).status_code == 200


def test_web_settings_never_return_secrets_and_live_needs_confirm(client):
    client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    r = client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H,
                    json={"direct_token": "tok-xyz", "direct_mode": "sandbox"})
    assert r.json() == {"ok": True}
    view = client.get("/p/crazy-drift/api/project", auth=AUTH).json()
    assert "tok-xyz" not in json.dumps(view)
    direct = next(b for b in view["blocks"] if b["id"] == "direct")
    tok = next(f for f in direct["fields"] if f["key"] == "direct_token")
    assert tok["set"] is True and tok["value"] == "" and direct["connected"] is True
    assert view["webhooks"]["tilda"].endswith(f"/p/crazy-drift/api/webhooks/tilda?token={view['webhook_token']}")
    assert client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H, json={"direct_mode": "live"}).status_code == 409
    assert client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H,
                       json={"direct_mode": "live", "confirm_live": True}).status_code == 200
    assert client.post("/api/project/settings", auth=AUTH, headers=H, json={}).status_code == 404  # вне проекта
    chk = client.post("/p/crazy-drift/api/project/check/topvisor", auth=AUTH, headers=H).json()
    assert chk["status"] == "skip"


def test_tilda_and_site_forms_go_to_their_project(client):
    client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    token = client.get("/p/crazy-drift/api/project", auth=AUTH).json()["webhook_token"]
    base = "/p/crazy-drift/api/webhooks"
    assert client.post(f"{base}/tilda?token=bad", data={"Phone": "1"}).status_code == 403
    assert client.post(f"{base}/tilda?token={token}", data={"test": "test"}).text == "ok"  # проверка Tilda
    r = client.post(f"{base}/tilda?token={token}", data={
        "Name": "Анна", "Phone": "+374 91 23-45-67", "tranid": "9001:42", "formid": "form1",
        "COOKIES": "utm_source=yandex; utm_campaign=777; _ym_uid=123"})
    assert r.text == "ok"
    assert client.post(f"{base}/form?token={token}", json={"id": "s1", "phone": "091000002"}).json() == {"ok": True}
    with P.use_project("crazy-drift"), session_scope() as s:
        touches = {t.system: t for t in s.query(Touch)}
    t = touches["tilda"]
    assert (t.phone, t.name, t.utm_source, t.utm_campaign, t.external_id) == ("+37491234567", "Анна", "yandex", "777", "tilda-9001:42")
    assert "site" in touches
    with session_scope() as s:
        assert s.query(Touch).count() == 0


def test_scheduler_tick_runs_each_project_once_per_day(monkeypatch, fixed_now):
    from partyhub import jobs
    from partyhub.projects import reset_current, set_current

    P.create_project("A", "proj-a")
    P.create_project("B", "proj-b")
    ran = []
    monkeypatch.setattr(jobs, "_run", lambda name: ran.append((P.current_slug(), name)))
    monkeypatch.setattr(jobs, "_ran_today", lambda name: False)
    state = {}
    for _ in range(2):
        for slug in jobs._project_slugs():
            token = set_current(slug)
            try:
                jobs._tick(slug, state)
            finally:
                reset_current(token)
    # 07:00: утренний сбор (06:00) пора, отчёт (09:00) ещё нет; заявки — раз в 10 минут
    assert sorted(ran) == [("proj-a", "daily"), ("proj-a", "leads"), ("proj-b", "daily"), ("proj-b", "leads")]


def test_metrika_and_topvisor_pickers(client, monkeypatch):
    from partyhub.collectors import metrika, topvisor

    client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    assert client.get("/p/crazy-drift/api/project/metrika/counters", auth=AUTH).status_code == 409  # нет токена
    client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H,
                json={"metrika_token": "m", "topvisor_user_id": "1", "topvisor_api_key": "k"})
    monkeypatch.setattr(metrika.MetrikaClient, "counters", lambda self: [{"id": "555", "name": "Сайт", "site": "crazy.example"}])
    monkeypatch.setattr(metrika.MetrikaClient, "goals", lambda self, c: [{"id": "9", "name": f"Заявка {c}", "type": "action"}])
    monkeypatch.setattr(topvisor.TopvisorClient, "projects", lambda self: [
        {"id": 7, "name": "Crazy", "searchers": [{"name": "Яндекс", "regions": [{"index": 213, "name": "Москва"}]}]}])
    assert client.get("/p/crazy-drift/api/project/metrika/counters", auth=AUTH).json()["counters"][0]["id"] == "555"
    assert client.get("/p/crazy-drift/api/project/metrika/goals?counter=555", auth=AUTH).json()["goals"][0]["name"] == "Заявка 555"
    assert client.get("/p/crazy-drift/api/project/metrika/goals?counter=x", auth=AUTH).status_code == 422
    tv = client.get("/p/crazy-drift/api/project/topvisor/projects", auth=AUTH).json()["projects"][0]
    assert tv["regions"] == [{"index": "213", "name": "Москва · Яндекс"}]


def test_metrika_several_counters_upload_to_first():
    from partyhub.collectors.metrika import MetrikaClient

    c = MetrikaClient("t", "111, 222")
    assert c.counter_id == "111,222" and c.counter_ids[0] == "111"


def test_archive_restore_and_purge_after_30_days(client):
    from datetime import timedelta

    from partyhub.jobs import _project_slugs

    P.create_project("Crazy Drift")
    P.create_project("Картинг")
    with P.use_project("karting"):
        with session_scope() as s:
            s.add(Touch(phone="+37400000000", source="form", external_id="t1", created_at=datetime(2026, 1, 1)))
    assert {p["slug"] for p in client.get("/api/projects", auth=AUTH).json()["projects"]} == {"crazy-drift", "karting"}
    assert client.post("/api/projects/karting/archive", auth=AUTH).status_code == 403  # без заголовка дашборда
    r = client.post("/api/projects/karting/archive", auth=AUTH, headers=H)
    assert r.status_code == 200 and r.json()["purge_at"]
    assert [p["slug"] for p in client.get("/api/projects", auth=AUTH).json()["projects"]] == ["crazy-drift"]
    arch = client.get("/api/projects?status=archived", auth=AUTH).json()["projects"]
    assert [p["slug"] for p in arch] == ["karting"] and arch[0]["purge_at"]
    assert _project_slugs() == ["crazy-drift"]  # архивный не собирается и не шлёт отчёты
    assert client.get("/p/karting/api/project", auth=AUTH).status_code == 200  # открыть по-прежнему можно

    archived_at = P.get_project("karting").archived_at
    assert P.purge_expired(archived_at + timedelta(days=29)) == []  # ещё рано
    client.post("/api/projects/karting/restore", auth=AUTH, headers=H)
    assert P.get_project("karting").archived_at is None
    assert P.purge_expired(archived_at + timedelta(days=31)) == []  # восстановленный не удаляется

    P.archive_project("karting")
    assert P.purge_expired(P.get_project("karting").archived_at + timedelta(days=30)) == ["karting"]
    assert P.get_project("karting") is None
    P.create_project("Картинг")  # данные ушли вместе с проектом
    with P.use_project("karting"):
        with session_scope() as s:
            assert s.query(Touch).count() == 0
    assert client.post("/api/projects/nope/archive", auth=AUTH, headers=H).status_code == 404


def test_all_projects_archived_does_not_fall_back_to_env_database():
    from partyhub.jobs import _project_slugs

    assert _project_slugs() == [None]  # проектов нет — общая база, как раньше
    P.create_project("Crazy Drift")
    P.archive_project("crazy-drift")
    assert _project_slugs() == []


def test_platform_table_gets_archived_at_column(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "old-platform.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE platform_projects (id INTEGER PRIMARY KEY, slug VARCHAR(40) UNIQUE, name VARCHAR(200), "
                "created_at DATETIME, settings_json TEXT, secrets_enc TEXT, rules_json TEXT, version INTEGER)")
    con.execute("INSERT INTO platform_projects (slug, name, settings_json, secrets_enc, rules_json, version) "
                "VALUES ('old', 'Старый', '{}', '', '{}', 1)")
    con.commit()
    con.close()
    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{db}")
    get_settings.cache_clear()
    from partyhub.config import base_settings

    getattr(base_settings, "cache_clear", lambda: None)()
    P.reset_platform()
    assert [p.slug for p in P.list_projects()] == ["old"]
    P.archive_project("old")
    assert P.list_projects() == [] and P.get_project("old").archived_at


def test_collect_now_button(client, monkeypatch):
    from partyhub import jobs
    from partyhub.collectors import topvisor

    client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    st = client.get("/p/crazy-drift/api/project/collect", auth=AUTH).json()
    assert st["blocks"]["topvisor"] == [] and st["schedule"]["daily"]
    # без ключей сбор честно говорит «не настроено»
    r = client.post("/p/crazy-drift/api/project/collect/topvisor", auth=AUTH, headers=H).json()
    assert r["results"][0]["status"] == "skipped" and "Подключения" in r["results"][0]["message"]
    client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H,
                json={"topvisor_user_id": "1", "topvisor_api_key": "k", "topvisor_project_id": "7"})
    monkeypatch.setattr(topvisor.TopvisorCollector, "collect", lambda self, s, d1, d2: 42)
    r = client.post("/p/crazy-drift/api/project/collect/topvisor", auth=AUTH, headers=H).json()
    assert r["results"][0]["status"] == "ok" and r["results"][0]["rows"] == 42 and r["days"] == 30
    last = client.get("/p/crazy-drift/api/project/collect", auth=AUTH).json()["blocks"]["topvisor"][0]
    assert last["status"] == "ok" and last["rows"] == 42
    assert client.post("/p/crazy-drift/api/project/collect/general", auth=AUTH, headers=H).status_code == 404
    assert client.post("/p/crazy-drift/api/project/collect/topvisor", auth=AUTH).status_code == 403


def test_bad_timezone_is_rejected_and_scheduler_survives(monkeypatch, capsys):
    from partyhub import jobs

    P.create_project("TZ", "proj-tz")
    with pytest.raises(ValueError, match="часовой пояс"):
        P.update_settings("proj-tz", {"timezone": "Moskoy"})
    with pytest.raises(ValueError, match="две латинские"):
        P.update_settings("proj-tz", {"phone_region": "Россия"})
    P.update_settings("proj-tz", {"timezone": "Europe/Moscow"})
    # даже если неверный пояс уже лежит в базе, журнал планировщика не падает
    monkeypatch.setattr(jobs, "now_local", lambda: (_ for _ in ()).throw(KeyError("Moskoy")))
    assert "UTC" in jobs._stamp()


def test_topvisor_schedule_read_and_edit(client, monkeypatch):
    from partyhub.collectors import topvisor

    client.post("/api/projects", auth=AUTH, headers=H, json={"name": "Crazy Drift"})
    assert client.get("/p/crazy-drift/api/project/topvisor/schedule", auth=AUTH).status_code == 409  # нет ключа
    client.post("/p/crazy-drift/api/project/settings", auth=AUTH, headers=H,
                json={"topvisor_user_id": "1", "topvisor_api_key": "k", "topvisor_project_id": "7"})
    calls = []
    stored = {"7": [{"days": ["MONDAY"], "times": [{"hour": 0, "minute": 0}]}]}

    def call(self, method, body):
        calls.append((method, body))
        if method == "get/projects_2/projects":
            return [{"id": 7, "name": "Crazy", "site": "crazy.example"}]
        if method == "get/schedule_2":
            return {"schedule": stored.get(str(body["target_id"]), []), "settings": []}
        if method == "edit/schedule_2":
            stored[str(body["target_id"])] = body["schedule"]
        if method == "del/schedule_2":
            stored.pop(str(body["target_id"]), None)
        return {}

    monkeypatch.setattr(topvisor.TopvisorClient, "call", call)
    got = client.get("/p/crazy-drift/api/project/topvisor/schedule", auth=AUTH).json()
    assert got["projects"] == [{"id": "7", "name": "Crazy", "site": "crazy.example", "schedule": stored["7"]}]

    url = "/p/crazy-drift/api/project/topvisor/schedule"
    r = client.post(url, auth=AUTH, headers=H, json={"project_id": "7", "days": ["THURSDAY", "MONDAY"], "hour": 3, "minute": 30})
    assert r.status_code == 200
    assert calls[-1] == ("edit/schedule_2", {"type": "positions_go", "target_id": 7,
                                             "schedule": [{"days": ["MONDAY", "THURSDAY"], "times": [{"hour": "3", "minute": "30"}]}]})
    every = list(topvisor.WEEKDAYS)
    client.post(url, auth=AUTH, headers=H, json={"project_id": "7", "days": every, "hour": 1, "minute": 0})
    assert calls[-1][1]["schedule"][0]["days"] == ["ALL"]
    client.post(url, auth=AUTH, headers=H, json={"project_id": "7", "days": [], "hour": 0, "minute": 0})
    assert calls[-1] == ("del/schedule_2", {"type": "positions_go", "target_id": 7}) and "7" not in stored
    # чужой проект, кривые дни и время, запрос без заголовка дашборда — отказ, в Топвизор ничего не уходит
    n = len(calls)
    assert client.post(url, auth=AUTH, headers=H, json={"project_id": "8", "days": ["MONDAY"]}).status_code == 422
    assert client.post(url, auth=AUTH, headers=H, json={"project_id": "7", "days": ["FUNDAY"]}).status_code == 422
    assert client.post(url, auth=AUTH, headers=H, json={"project_id": "7", "days": ["MONDAY"], "hour": 24}).status_code == 422
    assert client.post(url, auth=AUTH, json={"project_id": "7", "days": ["MONDAY"]}).status_code == 403
    assert len(calls) == n
