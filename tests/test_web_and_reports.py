from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from partyhub.models import Action, Touch
from partyhub.reports.daily import build_report, send_report
from partyhub.reports.telegram import action_buttons, split_message

AUTH = ("admin", "secret")


@pytest.fixture
def client(fixed_now):
    from partyhub.web.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def demo(fixed_now):
    from partyhub.db import session_scope
    from partyhub.demo import build_demo

    with session_scope() as s:
        build_demo(s, end=date(2026, 10, 20), days=45)


def test_dashboard_requires_login(client):
    assert client.get("/").status_code == 401
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200 and "Пульт маркетинга" in r.text
    assert client.get("/api/datasets", auth=("admin", "wrong")).status_code == 401


def test_datasets_have_every_dashboard_source(client, demo):
    d = client.get("/api/datasets?days=30&end=2026-10-20", auth=AUTH).json()
    for key in ("kpi", "daily", "sources", "placements", "campaigns", "leads", "funnel", "managers", "seo_keywords", "seo_history", "journal"):
        assert key in d, key
    assert len(d["daily"]) == 30
    kpi = {r["metric"]: r for r in d["kpi"]}
    assert kpi["period_days"]["value"] == 30
    leads_direct = sum(1 for l in d["leads"] if l["source"].startswith("Директ") and l["status_code"] != "spam")
    assert kpi["leads_direct"]["value"] == leads_direct
    assert all("•" in l["phone"] or l["phone"] == "—" for l in d["leads"])  # телефоны скрыты


def test_form_webhook_creates_touch_and_lead(client):
    assert client.post("/api/webhooks/form?token=bad", json={"phone": "091234567"}).status_code == 403
    assert client.post("/api/webhooks/form?token=hook-token-1", json={"name": "Без телефона"}).status_code == 422
    r = client.post("/api/webhooks/form?token=hook-token-1", json={"id": "f1", "phone": "091 23 45 67", "name": "Мариам", "utm_source": "yandex",
                                                           "utm_medium": "cpc", "utm_campaign": "701", "child_age": 7})
    assert r.json() == {"ok": True}
    assert client.post("/api/webhooks/form?token=hook-token-1", json={"id": "f1", "phone": "091234567"}).json()["duplicate"] is True
    from partyhub.db import session_scope
    from partyhub.models import Lead
    from partyhub.web.app import rebuilder

    rebuilder.run_now()  # в работе пересборка идёт с задержкой, чтобы не пересобирать на каждую форму

    with session_scope() as s:
        t = s.query(Touch).one()
        assert t.phone == "+37491234567" and t.extra == {"id": "f1", "child_age": 7}
        assert s.query(Lead).one().source_label == "Директ · Поиск"


def _proposed_action():
    from partyhub.db import session_scope

    with session_scope() as s:
        a = Action(created_at=datetime(2026, 10, 21, 7), rule="placement_high_bounce", level="approval", kind="exclude_site",
                   campaign_ids=[703], campaign_names=["РСЯ"], target="x.example", reason="Отказы 90%", metrics={}, status="proposed")
        s.add(a)
        s.flush()
        return a.id


H = {"X-Requested-With": "partyhub"}


def test_approve_and_rollback_from_dashboard(client):
    aid = _proposed_action()
    assert client.post(f"/api/actions/{aid}/approve", auth=AUTH).status_code == 403  # без заголовка дашборда
    assert client.post(f"/api/actions/{aid}/approve", auth=AUTH, headers={**H, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = client.post(f"/api/actions/{aid}/approve", auth=AUTH, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert client.post(f"/api/actions/{aid}/approve", auth=AUTH, headers=H).status_code == 409
    assert client.post(f"/api/actions/{aid}/rollback", auth=AUTH, headers=H).json()["status"] == "rolled_back"


def test_telegram_buttons_need_secret_and_right_chat(client, monkeypatch):
    sent = []
    from partyhub.reports import telegram as tg

    monkeypatch.setattr(tg.Telegram, "answer_callback", lambda self, cid, text: sent.append(text))
    aid = _proposed_action()
    update = {"callback_query": {"id": "c1", "data": f"rj:{aid}", "from": {"first_name": "Арам"}, "message": {"chat": {"id": 99}}}}
    assert client.post("/api/telegram", json=update).status_code == 403
    client.post("/api/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})
    assert sent[-1] == "Этот чат не может управлять правками"
    update["callback_query"]["message"]["chat"]["id"] = 42
    client.post("/api/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})
    assert sent[-1] == "Отклонено"
    from partyhub.db import session_scope

    with session_scope() as s:
        a = s.get(Action, aid)
        assert a.status == "rejected" and a.decided_by == "Арам (Telegram)"


def test_daily_report_sections_and_buttons(demo, session):
    text, ids = build_report(session, date(2026, 10, 20), with_ai=False)
    assert text.startswith("<b>")
    assert "Расход:" in text and "За 30 дней:" in text
    pending = session.query(Action).filter_by(status="proposed").count()
    assert len(ids) == min(pending, 10)
    if pending:
        assert "Ждут вашего решения" in text


def test_report_saved_even_without_telegram(demo, session):
    log = send_report(session, date(2026, 10, 20))
    assert log.delivered is False and "не настроен" in log.error and log.text


def test_split_message_and_buttons():
    parts = split_message("а" * 3000 + "\n\n" + "б" * 3000)
    assert len(parts) == 2 and all(len(p) <= 4000 for p in parts)
    assert action_buttons([5])[0][0]["callback_data"] == "ap:5"


def test_daily_job_skips_unconfigured_sources(session, fixed_now):
    from partyhub.jobs import daily
    from partyhub.models import CollectorRun

    res = daily(date(2026, 10, 20))
    assert {c["status"] for c in res["collect"]} == {"skipped"}
    from partyhub.jobs import COLLECTORS

    assert session.query(CollectorRun).count() == len(COLLECTORS)
    assert res["rules"]["proposed"] == 0
