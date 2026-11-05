"""Регрессионные тесты на находки независимого ревью."""
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from partyhub.collectors.direct import DirectClient, DirectError, parse_tsv
from partyhub.models import Action, AdStat, CampaignSnapshot, CrmRecord, Lead, SearchQuery, Touch
from partyhub.pipeline import metrics as M
from partyhub.pipeline.merge import rebuild_leads
from partyhub.rules.actions import ActionApplier, ActionError
from partyhub.rules.engine import run_rules, word_pattern
from partyhub.rules.state import excluded_pairs

DAY = date(2026, 10, 20)


class FakeDirect:
    def __init__(self, campaigns):
        self.campaigns = campaigns
        self.fail_on = set()

    def get_campaigns(self, ids=None):
        return [self.campaigns[i] for i in (ids or self.campaigns)]

    def get_campaign(self, cid):
        return self.campaigns[cid]

    def set_excluded_sites(self, cid, sites):
        if cid in self.fail_on:
            raise DirectError(8300, "Invalid object status")
        self.campaigns[cid]["ExcludedSites"] = {"Items": list(sites)} if sites else None

    def set_negative_keywords(self, cid, words):
        self.campaigns[cid]["NegativeKeywords"] = {"Items": list(words)} if words else None


def camp(cid, sites=None, words=None):
    return {"Id": cid, "ExcludedSites": {"Items": sites} if sites else None, "NegativeKeywords": {"Items": words} if words else None}


def action(session, now, target="x.example", cids=(1, 2), status="proposed", kind="exclude_site"):
    a = Action(created_at=now, rule="r", level="auto", kind=kind, campaign_ids=list(cids), campaign_names=["c"] * len(cids),
               target=target, reason="", metrics={}, status=status)
    session.add(a)
    session.flush()
    return a


# ------------------------------------------------------------- секреты
def test_empty_secrets_block_access(monkeypatch, fixed_now):
    from partyhub import config

    for k in ("DASHBOARD_PASSWORD", "WEBHOOK_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(k, "")
    config.get_settings.cache_clear()
    from partyhub.web.app import app

    with TestClient(app) as c:
        assert c.get("/", auth=("admin", "")).status_code == 503
        assert c.post("/api/webhooks/form?token=", json={"phone": "091234567"}).status_code == 503
        cb = {"callback_query": {"id": "1", "data": "ap:1"}}
        assert c.post("/api/telegram", json=cb).status_code == 503


# ------------------------------------------------------------- Директ API
def test_per_item_errors_are_raised():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/json/v501/campaigns")
        assert body["params"]["Campaigns"][0]["ExcludedSites"] is None  # пустой список очищается через null
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Errors": [{"Code": 8300, "Message": "Invalid object status"}]}]}})

    c = DirectClient("T", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(DirectError) as e:
        c.set_excluded_sites(1, [])
    assert e.value.code == 8300


def test_reports_use_v5_and_campaigns_v501():
    c = DirectClient("T", sandbox=False)
    assert c.url("reports").endswith("/json/v5/reports")
    assert c.url("campaigns").endswith("/json/v501/campaigns")


def test_tsv_with_quote_does_not_swallow_rows():
    text = 'CampaignName\tCost\n"Акция" для детей\t100\nДругая\t200\n'
    rows = parse_tsv(text)
    assert len(rows) == 2 and rows[1]["Cost"] == "200"


# ------------------------------------------------------------- применение и откат
def test_rollback_only_touches_campaigns_changed_by_platform(session, fixed_now):
    fake = FakeDirect({1: camp(1, ["x.example"]), 2: camp(2)})  # в кампании 1 площадка была исключена вручную
    a = action(session, fixed_now)
    ap = ActionApplier(session, mode="live", client=fake)
    ap.apply(a)
    assert a.status == "applied" and a.metrics["changed_campaigns"] == [2]
    ap.rollback(a, by="test")
    assert fake.campaigns[1]["ExcludedSites"]["Items"] == ["x.example"]  # ручное исключение осталось
    assert fake.campaigns[2]["ExcludedSites"] is None


def test_rollback_refused_when_mode_changed(session, fixed_now):
    fake = FakeDirect({1: camp(1), 2: camp(2)})
    a = action(session, fixed_now)
    ActionApplier(session, mode="live", client=fake).apply(a)
    with pytest.raises(ActionError):
        ActionApplier(session, mode="sandbox", client=fake).rollback(a, by="test")
    assert a.status == "applied" and fake.campaigns[1]["ExcludedSites"]["Items"] == ["x.example"]


def test_partial_failure_is_recorded_and_retryable(session, fixed_now):
    fake = FakeDirect({1: camp(1), 2: camp(2)})
    fake.fail_on = {2}
    a = action(session, fixed_now)
    ap = ActionApplier(session, mode="live", client=fake)
    ap.apply(a)
    assert a.status == "partial" and a.metrics["changed_campaigns"] == [1] and "8300" in a.result
    ap.rollback(a, by="test")
    assert a.status == "rolled_back" and fake.campaigns[1]["ExcludedSites"] is None


def test_apply_verifies_change_in_direct(session, fixed_now):
    class Silent(FakeDirect):
        def set_excluded_sites(self, cid, sites):  # Директ «принял», но ничего не изменил
            pass

    a = action(session, fixed_now, cids=(1,))
    ActionApplier(session, mode="live", client=Silent({1: camp(1)})).apply(a)
    assert a.status == "failed" and "не видно" in a.result


# ------------------------------------------------------------- dry_run → live
def _junk_stats(session, cid=703):
    for i in range(5):
        session.add(AdStat(date=DAY - timedelta(days=i), campaign_id=cid, campaign_name=f"C{cid}", network="AD_NETWORK",
                           placement="junk.example", impressions=1600, clicks=12, cost=4000))


def test_dry_run_history_does_not_count_after_switch_to_live(session, fixed_now, monkeypatch):
    from partyhub import config

    _junk_stats(session)
    session.flush()
    run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    assert session.query(Action).filter_by(status="applied", apply_mode="dry_run").count() == 1
    assert (703, "junk.example") in excluded_pairs(session)  # в dry_run дашборд показывает её исключённой

    monkeypatch.setenv("DIRECT_MODE", "live")
    config.get_settings.cache_clear()
    assert (703, "junk.example") not in excluded_pairs(session)  # в настоящем кабинете её никто не исключал
    fake = FakeDirect({703: camp(703)})
    res = run_rules(session, ActionApplier(session, mode="live", client=fake), day=DAY)
    assert res["auto_applied"] == 1 and fake.campaigns[703]["ExcludedSites"]["Items"] == ["junk.example"]


def test_exclusion_is_per_campaign(session, fixed_now):
    _junk_stats(session, 703)
    _junk_stats(session, 704)
    session.add(CampaignSnapshot(date=DAY, campaign_id=703, campaign_name="C703", data={"ExcludedSites": ["junk.example"]}))
    session.add(CampaignSnapshot(date=DAY, campaign_id=704, campaign_name="C704", data={"ExcludedSites": []}))
    session.flush()
    run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    a = session.query(Action).one()
    assert a.campaign_ids == [704] and a.campaign_names == ["C704"]


def test_unknown_placement_is_never_proposed(session, fixed_now):
    for i in range(5):
        session.add(AdStat(date=DAY - timedelta(days=i), campaign_id=1, campaign_name="C", network="AD_NETWORK",
                           placement="unknown", impressions=5000, clicks=50, cost=20000))
    session.flush()
    run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    assert session.query(Action).count() == 0


def test_rules_hold_auto_actions_when_lead_sources_are_stale(session, fixed_now):
    _junk_stats(session)
    session.flush()
    res = run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY, allow_auto=False, hold_reason="Не обновились заявки")
    a = session.query(Action).one()
    assert res["auto_applied"] == 0 and a.status == "proposed" and a.level == "approval" and "Не обновились" in a.reason


# ------------------------------------------------------------- минус-слова
def test_negative_word_boundaries_and_operators(session, fixed_now):
    pat = word_pattern("!работа")
    assert pat.search("картинг работа") and pat.search("работа, картинг")
    assert not pat.search("режим работы картинга") and not pat.search("подработа")
    session.add(SearchQuery(date=DAY, campaign_id=1, campaign_name="C", query="картинг работа", impressions=5, clicks=1, cost=300))
    session.add(SearchQuery(date=DAY, campaign_id=2, campaign_name="D", query="картинг работа", impressions=5, clicks=1, cost=300))
    session.add(CampaignSnapshot(date=DAY, campaign_id=2, campaign_name="D", data={"NegativeKeywords": ["!работа"]}))
    session.flush()
    run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    a = session.query(Action).filter_by(kind="add_negative").one()
    assert a.target == "!работа" and a.campaign_ids == [1]


# ------------------------------------------------------------- заявки и метрики
def test_late_booking_does_not_attach_to_old_lead(session):
    t0 = datetime(2026, 1, 10, 12)
    session.add(Touch(source="call", system="t", external_id="1", created_at=t0, phone="+37491000050"))
    session.add(CrmRecord(entity="deal", external_id="old", phone="+37491000050", status="lost", created_at=t0))
    session.add(CrmRecord(entity="deal", external_id="new", phone="+37491000050", status="done", amount=150000,
                          created_at=datetime(2026, 7, 1, 12)))
    session.flush()
    rebuild_leads(session)
    leads = session.query(Lead).order_by(Lead.first_touch_at).all()
    assert [l.status for l in leads] == ["lost", "done"] and leads[1].channel == "crm"


def test_chat_without_phone_links_to_its_crm_lead(session):
    t0 = datetime(2026, 10, 10, 12)
    session.add(Touch(source="chat", system="bitrix", external_id="bitrix-lead:77", created_at=t0, phone=None,
                      utm_source="yandex", utm_medium="cpc", utm_campaign="703", utm_content="context|kids.am"))
    session.add(CrmRecord(entity="lead", external_id="77", phone=None, status="booked", created_at=t0))
    session.flush()
    assert rebuild_leads(session) == 1
    lead = session.query(Lead).one()
    assert lead.status == "booked" and lead.placement == "kids.am"


def test_renamed_campaign_is_one_row(session):
    session.add(AdStat(date=date(2026, 10, 1), campaign_id=5, campaign_name="Старое имя", network="SEARCH", placement="search",
                       impressions=10, clicks=1, cost=100))
    session.add(AdStat(date=date(2026, 10, 2), campaign_id=5, campaign_name="Новое имя", network="SEARCH", placement="search",
                       impressions=10, clicks=1, cost=100))
    session.flush()
    rows = M.campaigns(session, date(2026, 10, 1), date(2026, 10, 2))
    assert len(rows) == 1 and rows[0]["campaign"] == "Новое имя" and rows[0]["cost"] == 200


def test_conversion_key_survives_lead_to_deal(session, fixed_now, monkeypatch):
    from partyhub.collectors import metrika

    t0 = datetime(2026, 10, 15, 12)
    session.add(Touch(source="form", system="site", external_id="f9", created_at=t0, phone="+37491000060", ym_client_id="123"))
    session.add(CrmRecord(entity="lead", external_id="7", phone="+37491000060", status="booked", created_at=t0))
    session.flush()
    rebuild_leads(session)

    class Client:
        def __init__(self):
            self.rows = []

        def upload_offline_conversions(self, rows, id_type):
            self.rows += rows
            return {"uploading": {"id": 1}}

    c = Client()
    monkeypatch.setattr(metrika, "now_local", lambda: fixed_now)
    assert metrika.upload_conversions(session, client=c)["uploaded"] == 1
    session.add(CrmRecord(entity="deal", external_id="9", phone="+37491000060", status="done", amount=90000,
                          created_at=t0 + timedelta(hours=2)))
    session.flush()
    rebuild_leads(session)
    assert metrika.upload_conversions(session, client=c)["uploaded"] == 0
    assert len(c.rows) == 1
