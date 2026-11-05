from datetime import date, datetime, timedelta

from partyhub.config import load_rules
from partyhub.models import Action, AdStat, CampaignSnapshot, Lead, SearchQuery, Touch, WebStat
from partyhub.pipeline.merge import rebuild_leads
from partyhub.rules.actions import ActionApplier, evaluate_due
from partyhub.rules.engine import is_mobile_app, run_rules

DAY = date(2026, 10, 20)


def stat(session, placement, cost, clicks, impressions, days=5, cid=703, network="AD_NETWORK"):
    for i in range(days):
        session.add(AdStat(date=DAY - timedelta(days=i), campaign_id=cid, campaign_name="РСЯ", network=network, placement=placement,
                           impressions=impressions // days, clicks=clicks // days, cost=cost / days))


def web(session, placement, visits, bounces):
    session.add(WebStat(date=DAY, utm_source="yandex", utm_medium="cpc", utm_campaign="703", utm_content=f"context|{placement}",
                        placement=placement, visits=visits, bounces=bounces, duration_sum=visits * 10))


class FakeDirect:
    """Подмена API Директа: хранит списки исключённых площадок и минус-слов."""

    def __init__(self):
        self.campaigns = {703: {"Id": 703, "ExcludedSites": {"Items": ["old.example"]}, "NegativeKeywords": {"Items": ["бесплатно"]}},
                          701: {"Id": 701, "ExcludedSites": None, "NegativeKeywords": None}}
        self.calls = []

    def get_campaigns(self, ids=None):
        return [self.campaigns[i] for i in (ids or self.campaigns)]

    def get_campaign(self, cid):
        return self.campaigns[cid]

    def set_excluded_sites(self, cid, sites):
        self.calls.append(("sites", cid, list(sites)))
        self.campaigns[cid]["ExcludedSites"] = {"Items": list(sites)}

    def set_negative_keywords(self, cid, words):
        self.calls.append(("negatives", cid, list(words)))
        self.campaigns[cid]["NegativeKeywords"] = {"Items": list(words)}


def test_mobile_app_detection():
    assert is_mobile_app("com.puzzle.blast.free")
    assert is_mobile_app("id1458963210")
    assert not is_mobile_app("mama-club.am")
    assert not is_mobile_app("dzen.ru")


def test_rules_find_bad_placements_and_respect_protected(session, fixed_now):
    target = load_rules()["economics"]["target_cpl"]
    stat(session, "junk.example", cost=target * 2, clicks=60, impressions=8000)          # дорого и без заявок
    stat(session, "com.game.app", cost=3000, clicks=40, impressions=800)                  # приложение, CTR 5%
    stat(session, "bouncy.example", cost=2000, clicks=40, impressions=5000)               # дёшево, но отказы
    web(session, "bouncy.example", visits=35, bounces=30)
    stat(session, "dzen.ru", cost=target * 3, clicks=200, impressions=30000)              # в списке защищённых
    stat(session, "good.example", cost=target * 3, clicks=200, impressions=30000)         # есть заявка
    session.add(Touch(source="form", system="t", external_id="1", created_at=datetime(2026, 10, 19, 12), phone="+37491111111",
                      utm_source="yandex", utm_medium="cpc", utm_campaign="703", utm_content="context|good.example"))
    session.flush()
    rebuild_leads(session)
    res = run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    acts = {a.target: a for a in session.query(Action)}
    assert set(acts) == {"junk.example", "com.game.app", "bouncy.example"}
    assert acts["junk.example"].rule == "placement_no_leads" and acts["junk.example"].status == "applied"
    assert acts["com.game.app"].rule == "mobile_app_clicks" and acts["com.game.app"].status == "applied"
    assert acts["bouncy.example"].level == "approval" and acts["bouncy.example"].status == "proposed"
    assert res["auto_applied"] == 2 and res["awaiting_approval"] == 1
    assert "dry-run" in acts["junk.example"].result


def test_rules_do_not_repeat(session, fixed_now):
    stat(session, "junk.example", cost=20000, clicks=60, impressions=8000)
    session.flush()
    applier = ActionApplier(session, mode="dry_run")
    run_rules(session, applier, day=DAY)
    run_rules(session, applier, day=DAY)
    assert session.query(Action).count() == 1


def test_rejected_proposal_is_not_offered_again(session, fixed_now):
    stat(session, "bouncy.example", cost=2000, clicks=40, impressions=5000)
    web(session, "bouncy.example", visits=35, bounces=30)
    session.flush()
    applier = ActionApplier(session, mode="dry_run")
    run_rules(session, applier, day=DAY)
    a = session.query(Action).one()
    applier.reject(a, by="test")
    run_rules(session, applier, day=DAY)
    assert session.query(Action).count() == 1  # отклонённое не предлагается 30 дней


def test_daily_limit_moves_extra_auto_actions_to_approval(session, fixed_now, monkeypatch):
    rules = load_rules()
    monkeypatch.setitem(rules["limits"], "max_auto_actions_per_day", 2)
    for i in range(4):
        stat(session, f"junk{i}.example", cost=20000, clicks=60, impressions=8000)
    session.flush()
    res = run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    assert res["auto_applied"] == 2 and res["deferred_by_limit"] == 2
    assert session.query(Action).filter_by(status="proposed").count() == 2


def test_negative_keywords_skip_campaigns_that_already_have_word(session, fixed_now):
    for cid in (701, 702):
        session.add(SearchQuery(date=DAY, campaign_id=cid, campaign_name=f"C{cid}", query="картинг вакансии", impressions=10, clicks=2, cost=800))
    session.add(CampaignSnapshot(date=DAY, campaign_id=702, campaign_name="C702", data={"NegativeKeywords": ["-вакансии"]}))
    session.flush()
    run_rules(session, ActionApplier(session, mode="dry_run"), day=DAY)
    a = session.query(Action).filter_by(kind="add_negative").one()
    assert a.target == "вакансии" and a.campaign_ids == [701]


def test_apply_and_rollback_via_api_client(session, fixed_now):
    fake = FakeDirect()
    a = Action(created_at=fixed_now, rule="r", level="auto", kind="exclude_site", campaign_ids=[703, 701], campaign_names=["a", "b"],
               target="junk.example", reason="", metrics={}, status="proposed")
    session.add(a)
    session.flush()
    applier = ActionApplier(session, mode="sandbox", client=fake)
    applier.apply(a)
    assert a.status == "applied" and a.apply_mode == "sandbox"
    assert fake.campaigns[703]["ExcludedSites"]["Items"] == ["old.example", "junk.example"]
    assert fake.campaigns[701]["ExcludedSites"]["Items"] == ["junk.example"]
    applier.rollback(a, by="test")
    assert a.status == "rolled_back"
    assert fake.campaigns[703]["ExcludedSites"]["Items"] == ["old.example"]


def test_dry_run_never_touches_api(session, fixed_now):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("в режиме dry_run API вызываться не должен")

    a = Action(created_at=fixed_now, rule="r", level="auto", kind="add_negative", campaign_ids=[701], campaign_names=["a"],
               target="работа", reason="", metrics={}, status="proposed")
    session.add(a)
    session.flush()
    ActionApplier(session, mode="dry_run", client=Boom()).apply(a)
    assert a.status == "applied"


def test_failed_api_call_marks_action_failed(session, fixed_now):
    class Broken(FakeDirect):
        def set_excluded_sites(self, cid, sites):
            raise RuntimeError("limit exceeded")

    a = Action(created_at=fixed_now, rule="r", level="auto", kind="exclude_site", campaign_ids=[703], campaign_names=["a"],
               target="x.example", reason="", metrics={}, status="proposed")
    session.add(a)
    session.flush()
    ActionApplier(session, mode="live", client=Broken()).apply(a)
    assert a.status == "failed" and "limit exceeded" in a.result


def test_evaluation_after_seven_days(session, fixed_now):
    applied = datetime(2026, 10, 10, 7)
    a = Action(created_at=applied, rule="r", level="auto", kind="exclude_site", campaign_ids=[703], campaign_names=["a"],
               target="x", reason="", metrics={}, status="applied", applied_at=applied, eval_due=date(2026, 10, 17))
    session.add(a)
    for i in range(1, 8):
        session.add(AdStat(date=date(2026, 10, 10) - timedelta(days=i), campaign_id=703, campaign_name="a", network="AD_NETWORK", placement="p", impressions=1, clicks=1, cost=10000))
        session.add(AdStat(date=date(2026, 10, 10) + timedelta(days=i - 1), campaign_id=703, campaign_name="a", network="AD_NETWORK", placement="p", impressions=1, clicks=1, cost=6000))
    for i, day in enumerate([date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 12), date(2026, 10, 14)]):
        session.add(Lead(phone=f"+3749100009{i}", first_touch_at=datetime.combine(day, datetime.min.time()), last_touch_at=datetime.combine(day, datetime.min.time()),
                         channel="call", source_label="Директ · РСЯ", campaign_id=703, status="new"))
    session.flush()
    done = evaluate_due(session, today=date(2026, 10, 17))
    assert len(done) == 1
    r = a.eval_result
    assert r["before"]["cpl"] == 35000 and r["after"]["cpl"] == 21000 and r["verdict"] == "better"
