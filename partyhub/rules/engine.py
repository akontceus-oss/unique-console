"""Модуль правил: находит, что поправить в кампаниях, и раскладывает правки по уровням.

auto     — платформа применяет сама (в пределах дневного лимита);
approval — правка ждёт «да» в Telegram или в дашборде;
alert    — только сигнал в отчёте.

Каждое предложение хранит причину и цифры, на которых оно основано.
Исключения площадок считаются по кампаниям: площадка, исключённая в одной кампании,
может быть предложена к исключению в другой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings, load_rules
from ..models import Action, AdStat, Lead, SearchQuery, WebStat
from ..pipeline.normalize import now_local, yesterday
from .state import clean_word, current_negatives, excluded_pairs

APP_RE = re.compile(r"^(com|ru|net|org|io|air|me|app|am|tv|co)\.[a-z0-9_\-]+\.[a-z0-9_.\-]+$")
IOS_RE = re.compile(r"^(id)?\d{6,}$")
# Служебные значения отчёта Директа, а не площадки: их исключать нельзя.
NOT_PLACEMENTS = {"", "unknown", "search", "none", "--"}


def is_mobile_app(placement: str) -> bool:
    """Площадки-приложения в отчётах Директа выглядят как bundle id (com.example.game) или id App Store."""
    p = (placement or "").lower().strip()
    return bool(APP_RE.match(p) or IOS_RE.match(p))


@dataclass
class Proposal:
    rule: str
    level: str
    kind: str
    target: str
    reason: str
    campaign_ids: list[int] = field(default_factory=list)
    campaign_names: list[str] = field(default_factory=list)  # в том же порядке, что campaign_ids
    metrics: dict = field(default_factory=dict)


def _money(v: float) -> str:
    s = get_settings().currency_symbol
    return f"{v:,.0f}".replace(",", " ") + f" {s}"


def _pct(v: float) -> str:
    s = f"{v * 100:.1f}".replace(".", ",")
    return (s[:-2] if s.endswith(",0") else s) + "%"


def _days(n: int) -> str:
    n100, n10 = n % 100, n % 10
    word = "дней" if 10 < n100 < 20 else "день" if n10 == 1 else "дня" if 1 < n10 < 5 else "дней"
    return f"{n} {word}"


def _num(v: float) -> str:
    return str(round(v, 1)).replace(".", ",")


# ---------------------------------------------------------- площадки РСЯ ---
def placement_proposals(session: Session, d1: date, d2: date) -> list[Proposal]:
    rules = load_rules()
    cfg = rules["rules"]
    target_cpl = rules["economics"]["target_cpl"]
    protected = {p.lower() for p in rules.get("protected_placements", [])}
    booked = set(rules["leads"]["booked_statuses"])

    ads = pd.read_sql(
        select(AdStat).where(AdStat.date.between(d1, d2), AdStat.network == "AD_NETWORK"), session.connection()
    )
    if ads.empty:
        return []
    ads["cost"] = ads["cost"].astype(float)
    ads["campaign_id"] = ads["campaign_id"].astype(int)
    web = pd.read_sql(select(WebStat).where(WebStat.date.between(d1, d2)), session.connection())
    leads = pd.read_sql(
        select(Lead.placement, Lead.status).where(
            Lead.first_touch_at >= datetime.combine(d1, datetime.min.time()),
            Lead.first_touch_at < datetime.combine(d2 + timedelta(days=1), datetime.min.time()),
            Lead.network == "AD_NETWORK",
            Lead.status != "spam",
        ),
        session.connection(),
    )
    ever_booked = {
        p for (p,) in session.execute(select(Lead.placement).where(Lead.status.in_(booked), Lead.placement.is_not(None))).all()
    }
    excluded = excluded_pairs(session)
    names = ads.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()

    out: list[Proposal] = []
    for placement, g_all in ads.groupby("placement"):
        key = str(placement).lower().strip()
        if key in NOT_PLACEMENTS or key in protected or key in ever_booked:
            continue
        n_leads = int((leads["placement"] == key).sum()) if not leads.empty else 0
        if n_leads:
            continue  # площадку с заявками не трогаем автоматически — её оценивает цена брони
        # Только кампании, где площадка ещё не исключена и тратила деньги.
        g = g_all[[(int(c), key) not in excluded for c in g_all["campaign_id"]]]
        g = g[g["campaign_id"].isin(g.groupby("campaign_id")["cost"].sum().loc[lambda s: s > 0].index)]
        if g.empty:
            continue
        cost, clicks, impr = float(g["cost"].sum()), int(g["clicks"].sum()), int(g["impressions"].sum())
        w = web[web["placement"] == key] if not web.empty else web
        visits = int(w["visits"].sum()) if not w.empty else 0
        bounce = (int(w["bounces"].sum()) / visits) if visits else None
        ctr = clicks / impr if impr else 0.0
        cids = sorted({int(c) for c in g["campaign_id"]})
        cnames = [str(names.get(c, c)) for c in cids]
        m = {
            "cost": round(cost, 2),
            "clicks": clicks,
            "impressions": impr,
            "ctr": round(ctr, 4),
            "visits": visits,
            "bounce_rate": round(bounce, 3) if bounce is not None else None,
            "leads": n_leads,
            "period": f"{d1.isoformat()}..{d2.isoformat()}",
        }

        r = cfg.get("mobile_app_clicks", {})
        if r.get("enabled") and is_mobile_app(key) and clicks >= r["min_clicks"] and ctr >= r["min_ctr"]:
            days = len(pd.unique(g["date"]))
            out.append(Proposal(
                "mobile_app_clicks", r["level"], "exclude_site", key,
                f"Приложение: CTR {_pct(ctr)} при {clicks} кликах и ни одной заявки за {_days(days)} — похоже на случайные клики. "
                f"Расход {_money(cost)}.",
                cids, cnames, m,
            ))
            continue
        r = cfg.get("placement_no_leads", {})
        if r.get("enabled") and cost >= r["cost_multiplier"] * target_cpl and clicks >= r["min_clicks"]:
            out.append(Proposal(
                "placement_no_leads", r["level"], "exclude_site", key,
                f"Потратила {_money(cost)} — это {_num(cost / target_cpl)} целевой цены заявки, {clicks} кликов, заявок нет.",
                cids, cnames, m,
            ))
            continue
        r = cfg.get("placement_high_bounce", {})
        if r.get("enabled") and bounce is not None and visits >= r["min_visits"] and bounce >= r["bounce_rate"]:
            out.append(Proposal(
                "placement_high_bounce", r["level"], "exclude_site", key,
                f"Отказы {bounce * 100:.0f}% на {visits} визитах, заявок нет. Расход {_money(cost)}.",
                cids, cnames, m,
            ))
    return out


# ---------------------------------------------------------- минус-слова ---
def word_pattern(word: str) -> re.Pattern:
    """Слово целиком: не часть другого слова, знаки препинания вокруг не мешают."""
    return re.compile(r"(?<![\w-])" + re.escape(clean_word(word)) + r"(?![\w-])", re.IGNORECASE)


def negative_keyword_proposals(session: Session, d1: date, d2: date) -> list[Proposal]:
    """Стоп-слова из rules.yaml. Оператор «!» фиксирует форму слова: «!работа» не заблокирует «режим работы»."""
    r = load_rules()["rules"].get("negative_keywords", {})
    if not r.get("enabled"):
        return []
    q = pd.read_sql(select(SearchQuery).where(SearchQuery.date.between(d1, d2)), session.connection())
    if q.empty:
        return []
    q["cost"] = q["cost"].astype(float)
    q["campaign_id"] = q["campaign_id"].astype(int)
    names = q.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
    negatives = current_negatives(session)
    out = []
    for word in r["stop_words"]:
        clean = clean_word(word)
        pat = word_pattern(word)
        hit = q[q["query"].map(lambda s: bool(pat.search(str(s)))) & (q["clicks"] >= r.get("min_clicks", 1))]
        if hit.empty:
            continue
        cids = sorted({int(c) for c in hit["campaign_id"] if clean not in negatives.get(int(c), set())})
        if not cids:
            continue
        sub = hit[hit["campaign_id"].isin(cids)]
        examples = list(dict.fromkeys(sub.sort_values("cost", ascending=False)["query"]))[:3]
        cost = float(sub["cost"].sum())
        out.append(Proposal(
            "negative_keywords", r["level"], "add_negative", word,
            f"Запросы со словом «{clean}»: {int(sub['clicks'].sum())} кликов, {_money(cost)}. Примеры: "
            + "; ".join(f"«{e}»" for e in examples),
            cids, [str(names.get(c, c)) for c in cids],
            {"cost": round(cost, 2), "clicks": int(sub["clicks"].sum()), "examples": examples},
        ))
    return out


# ---------------------------------------------------------- всплески ---
def spend_spike_proposals(session: Session, day: date) -> list[Proposal]:
    r = load_rules()["rules"].get("spend_spike", {})
    if not r.get("enabled"):
        return []
    ads = pd.read_sql(
        select(AdStat.date, AdStat.campaign_id, AdStat.campaign_name, AdStat.cost).where(
            AdStat.date.between(day - timedelta(days=7), day)
        ),
        session.connection(),
    )
    if ads.empty:
        return []
    ads["cost"] = ads["cost"].astype(float)
    names = ads.sort_values("date").groupby("campaign_id")["campaign_name"].last().to_dict()
    out = []
    for cid, g in ads.groupby("campaign_id"):
        today_cost = float(g.loc[g["date"] == day, "cost"].sum())
        prev = g[g["date"] < day].groupby("date")["cost"].sum()
        if (prev > 0).sum() < 3:
            continue
        # Дни без расхода внутри периода входят в среднее; дни до запуска кампании — нет.
        first_day = min(prev[prev > 0].index)
        avg = float(prev.sum()) / max(1, (day - first_day).days)
        if avg <= 0:
            continue
        ratio = today_cost / avg
        if ratio >= r["ratio"]:
            name = names.get(cid, str(cid))
            out.append(Proposal(
                "spend_spike", "alert", "alert", f"{name} · {day.isoformat()}",
                f"Расход {_money(today_cost)} — в {_num(ratio)} раза выше среднего за 7 дней ({_money(avg)}).",
                [int(cid)], [name],
                {"cost": round(today_cost, 2), "avg7": round(avg, 2), "ratio": round(ratio, 2)},
            ))
    return out


# ---------------------------------------------------------- сохранение ---
def _covered_campaigns(session: Session, p: Proposal) -> set[int]:
    """Кампании, где такая правка уже есть, ждёт решения или недавно отклонена/откачена."""
    cooldown = load_rules()["limits"]["repeat_cooldown_days"]
    since = now_local() - timedelta(days=cooldown)
    mode = get_settings().direct_mode
    covered: set[int] = set()
    for a in session.scalars(select(Action).where(Action.kind == p.kind)):
        if clean_word(a.target) != clean_word(p.target) and a.target != p.target:
            continue
        cids = {int(c) for c in (a.campaign_ids or [])}
        if a.status in ("proposed", "approved", "alert"):
            covered |= cids
        elif a.status in ("applied", "partial") and a.apply_mode == mode:
            covered |= cids
        elif a.status == "rejected" and (a.decided_at or a.created_at) >= since:
            covered |= cids
        elif a.status == "rolled_back" and (a.rolled_back_at or a.created_at) >= since:
            covered |= cids
        # failed — не блокирует: правка будет предложена снова на следующий день
    return covered


def evaluate(session: Session, day: date | None = None) -> list[Proposal]:
    day = day or yesterday()
    lookback = load_rules()["limits"]["lookback_days"]
    d1 = day - timedelta(days=lookback - 1)
    return (
        placement_proposals(session, d1, day)
        + negative_keyword_proposals(session, d1, day)
        + spend_spike_proposals(session, day)
    )


def save_proposals(session: Session, proposals: list[Proposal], at: datetime | None = None) -> list[Action]:
    at = at or now_local()
    saved = []
    for p in proposals:
        covered = _covered_campaigns(session, p)
        keep = [(c, n) for c, n in zip(p.campaign_ids, p.campaign_names or [""] * len(p.campaign_ids)) if int(c) not in covered]
        if not keep:
            continue
        a = Action(
            created_at=at,
            rule=p.rule,
            level=p.level,
            kind=p.kind,
            campaign_ids=[int(c) for c, _ in keep],
            campaign_names=[n for _, n in keep],
            target=p.target,
            reason=p.reason,
            metrics=p.metrics,
            status="alert" if p.level == "alert" else "proposed",
        )
        session.add(a)
        saved.append(a)
    session.flush()
    return saved


def run_rules(session: Session, applier, day: date | None = None, at: datetime | None = None, allow_auto: bool = True,
              hold_reason: str = "") -> dict:
    """Оценить правила, сохранить предложения и применить автоматические в пределах лимита.

    После каждой правки транзакция фиксируется: запись о сделанном в Директе не должна потеряться,
    даже если дальше что-то упадёт. allow_auto=False — все правки только на одобрение
    (например, если источники заявок не обновились и картина может быть неполной).
    """
    at = at or now_local()
    saved = save_proposals(session, evaluate(session, day), at)
    session.commit()
    limit = load_rules()["limits"]["max_auto_actions_per_day"]
    start = datetime.combine(at.date(), datetime.min.time())
    applied_today = (
        session.query(Action)
        .filter(Action.level == "auto", Action.status.in_(["applied", "partial"]), Action.applied_at >= start)
        .count()
    )
    applied, deferred = [], []
    for a in saved:
        if a.level != "auto":
            continue
        if not allow_auto:
            a.level = "approval"
            a.reason += f" ({hold_reason or 'Автоправки приостановлены'} — нужно ваше решение.)"
            deferred.append(a)
            continue
        if applied_today >= limit:
            a.level = "approval"  # лимит исчерпан — переводим в ручное одобрение
            a.reason += " (Дневной лимит автоправок исчерпан — нужно ваше решение.)"
            deferred.append(a)
            continue
        applier.apply(a, by="auto", at=at)
        session.commit()
        if a.status in ("applied", "partial"):
            applied_today += 1
        applied.append(a)
    session.commit()
    return {
        "proposed": len(saved),
        "auto_applied": sum(1 for a in applied if a.status == "applied"),
        "partial": sum(1 for a in applied if a.status == "partial"),
        "failed": sum(1 for a in applied if a.status == "failed"),
        "awaiting_approval": sum(1 for a in saved if a.status == "proposed"),
        "alerts": sum(1 for a in saved if a.status == "alert"),
        "deferred_by_limit": len(deferred),
    }
