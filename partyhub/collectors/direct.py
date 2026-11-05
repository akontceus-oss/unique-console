"""Яндекс Директ API: отчёты, настройки кампаний и правки.

Документация: https://yandex.ru/dev/direct/doc/ru/start
Кампании — через адрес v501 (он нужен для Единой перфоманс-кампании), отчёты — через v5.
Песочница:    https://yandex.ru/dev/direct/doc/ru/concepts/sandbox — тестовые кампании
              и условная статистика, реальные данные не затрагиваются.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from datetime import date

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AdStat, CampaignSnapshot, DirectSliceStat, SearchQuery, SettingsChange
from .base import Collector

LIVE_HOST = "https://api.direct.yandex.com/json/"
SANDBOX_HOST = "https://api-sandbox.direct.yandex.com/json/"
SERVICE_VERSION = {"campaigns": "v501", "reports": "v5", "bidmodifiers": "v5"}


class DirectError(RuntimeError):
    def __init__(self, code, message, detail=""):
        super().__init__(f"Direct API error {code}: {message}. {detail}".strip())
        self.code, self.message, self.detail = code, message, detail


class DirectClient:
    def __init__(
        self,
        token: str,
        client_login: str = "",
        sandbox: bool = True,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        if not token:
            raise ValueError("Не задан DIRECT_TOKEN")
        self.host = SANDBOX_HOST if sandbox else LIVE_HOST
        self.base = self.host + "v5/"
        self.headers = {"Authorization": f"Bearer {token}", "Accept-Language": "ru"}
        if client_login:
            self.headers["Client-Login"] = client_login
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep

    # ------------------------------------------------------- общий вызов ---
    def url(self, service: str) -> str:
        return self.host + SERVICE_VERSION.get(service, "v5") + "/" + service

    def call(self, service: str, method: str, params: dict) -> dict:
        r = self.http.post(self.url(service), headers=self.headers, json={"method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            e = data["error"]
            raise DirectError(e.get("error_code"), e.get("error_string"), e.get("error_detail", ""))
        return data.get("result", {})

    # ------------------------------------------------------- отчёты ---
    def report(
        self,
        report_type: str,
        field_names: list[str],
        date_from: date,
        date_to: date,
        goals: list[int] | None = None,
        filters: list[dict] | None = None,
        max_wait_sec: int = 600,
    ) -> list[dict]:
        """Сервис Reports: офлайн-отчёт в TSV. Ждёт готовности по заголовку retryIn."""
        params = {
            "SelectionCriteria": {"DateFrom": date_from.isoformat(), "DateTo": date_to.isoformat()},
            "FieldNames": field_names,
            "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
        if filters:
            params["SelectionCriteria"]["Filter"] = filters
        if goals:
            params["Goals"] = goals
            params["AttributionModels"] = ["AUTO"]
        # Директ кэширует отчёт по имени и параметрам: час в имени даёт свежие данные при повторном запуске.
        stamp = time.strftime("%Y%m%d%H")
        params["ReportName"] = report_type + "-" + hashlib.md5((json.dumps(params, sort_keys=True) + stamp).encode()).hexdigest()[:12]
        headers = dict(
            self.headers,
            processingMode="auto",
            returnMoneyInMicros="false",
            skipReportHeader="true",
            skipReportSummary="true",
        )
        waited = 0
        while True:
            r = self.http.post(self.url("reports"), headers=headers, json={"params": params})
            if r.status_code == 200:
                return parse_tsv(r.text)
            if r.status_code in (201, 202):
                delay = int(r.headers.get("retryIn", "10"))
                waited += delay
                if waited > max_wait_sec:
                    raise TimeoutError(f"Отчёт {params['ReportName']} не готов за {max_wait_sec} с")
                self.sleep(delay)
                continue
            try:
                e = r.json().get("error", {})
            except ValueError:
                e = {"error_string": r.text[:300]}
            raise DirectError(e.get("error_code", r.status_code), e.get("error_string", "HTTP error"), e.get("error_detail", ""))

    # ------------------------------------------------------- кампании ---
    def get_campaigns(self, ids: list[int] | None = None) -> list[dict]:
        criteria = {"Ids": ids} if ids else {"States": ["ON", "OFF", "SUSPENDED", "ENDED"]}
        res = self.call(
            "campaigns",
            "get",
            {
                "SelectionCriteria": criteria,
                "FieldNames": ["Id", "Name", "Type", "State", "Status", "DailyBudget", "ExcludedSites", "NegativeKeywords"],
                "TextCampaignFieldNames": ["BiddingStrategy"],
                "UnifiedCampaignFieldNames": ["BiddingStrategy"],
            },
        )
        return res.get("Campaigns", [])

    def get_campaign(self, campaign_id: int) -> dict:
        """Одна кампания со списками; ошибка, если Директ её не вернул или не прислал нужные поля."""
        found = [c for c in self.get_campaigns([campaign_id]) if int(c.get("Id", 0)) == int(campaign_id)]
        if not found:
            raise DirectError("not_found", f"кампания {campaign_id} не найдена или недоступна этому токену")
        c = found[0]
        for key in ("ExcludedSites", "NegativeKeywords"):
            if key not in c:
                raise DirectError("no_field", f"Директ не вернул поле {key} для кампании {campaign_id}")
        return c

    def _update(self, campaign_id: int, field: str, values: list[str]) -> dict:
        """Пишет список целиком. Пустой список — null (так Директ очищает поле)."""
        value = {"Items": sorted(set(values))} if values else None
        res = self.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, field: value}]})
        results = res.get("UpdateResults") or []
        if not results:
            raise DirectError("empty_result", f"Директ не подтвердил изменение кампании {campaign_id}")
        errors = results[0].get("Errors") or []
        if errors:
            e = errors[0]
            raise DirectError(e.get("Code"), e.get("Message"), e.get("Details", ""))
        return results[0]

    def set_excluded_sites(self, campaign_id: int, sites: list[str]) -> dict:
        return self._update(campaign_id, "ExcludedSites", sites)

    def set_negative_keywords(self, campaign_id: int, words: list[str]) -> dict:
        return self._update(campaign_id, "NegativeKeywords", words)

    def update_campaign(self, campaign: dict) -> dict:
        """Произвольное изменение одной кампании (например, стратегии). campaign — с полем Id."""
        res = self.call("campaigns", "update", {"Campaigns": [campaign]})
        return _first_result(res, "UpdateResults", campaign.get("Id"))

    # ------------------------------------------------ корректировки ставок ---
    def get_bid_modifiers(self, campaign_id: int) -> list[dict]:
        res = self.call(
            "bidmodifiers",
            "get",
            {
                "SelectionCriteria": {"CampaignIds": [campaign_id], "Levels": ["CAMPAIGN"]},
                "FieldNames": ["Id", "CampaignId", "Type", "Level"],
                "MobileAdjustmentFieldNames": ["BidModifier"],
                "DesktopAdjustmentFieldNames": ["BidModifier"],
                "TabletAdjustmentFieldNames": ["BidModifier"],
                "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
            },
        )
        return res.get("BidModifiers", [])

    def add_bid_modifier(self, campaign_id: int, adjustment: dict) -> dict:
        """adjustment — например {"MobileAdjustment": {"BidModifier": 80}}."""
        res = self.call("bidmodifiers", "add", {"BidModifiers": [{"CampaignId": campaign_id, **adjustment}]})
        return _first_result(res, "AddResults", campaign_id)

    def set_bid_modifier(self, modifier_id: int, value: int) -> dict:
        res = self.call("bidmodifiers", "set", {"BidModifiers": [{"Id": modifier_id, "BidModifier": value}]})
        return _first_result(res, "SetResults", modifier_id)

    def delete_bid_modifier(self, modifier_id: int) -> dict:
        res = self.call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": [modifier_id]}})
        return _first_result(res, "DeleteResults", modifier_id)


def _first_result(res: dict, key: str, what) -> dict:
    results = res.get(key) or []
    if not results:
        raise DirectError("empty_result", f"Директ не подтвердил изменение ({what})")
    errors = results[0].get("Errors") or []
    if errors:
        e = errors[0]
        raise DirectError(e.get("Code"), e.get("Message"), e.get("Details", ""))
    return results[0]


def parse_tsv(text: str) -> list[dict]:
    """TSV-отчёт → список словарей. '--' в Директе означает «нет значения»."""
    text = text.strip("\n")
    if not text:
        return []
    # Директ отдаёт TSV без кавычек: кавычка в названии не должна «склеивать» строки.
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    rows = []
    for row in reader:
        rows.append({k: (None if v in ("--", "") else v) for k, v in row.items()})
    return rows


def items(value) -> list[str]:
    """{'Items': [...]} | None → список строк."""
    if isinstance(value, dict):
        return list(value.get("Items") or [])
    return list(value or [])


def client_from_settings(mode: str | None = None) -> DirectClient:
    s = get_settings()
    mode = mode or s.direct_mode
    return DirectClient(s.direct_token, s.direct_client_login, sandbox=(mode != "live"))


# ============================================================== сборщик ===
AD_FIELDS = [
    "Date",
    "CampaignId",
    "CampaignName",
    "AdGroupId",
    "AdGroupName",
    "AdNetworkType",
    "Placement",
    "Impressions",
    "Clicks",
    "Cost",
]
QUERY_FIELDS = ["Date", "CampaignId", "CampaignName", "AdGroupId", "Query", "Impressions", "Clicks", "Cost"]
# Чем подобран запрос (ключ или автотаргетинг) и категория автотаргетинга. Если Директ не примет
# эти поля, отчёт перезапрашивается без них — статистика запросов важнее разметки.
QUERY_EXTRA_FIELDS = ["CriterionType", "Criterion", "TargetingCategory"]
# Срезы для корректировок ставок: (срез, поля отчёта).
SLICE_REPORTS = {"device": ["Device"], "demography": ["Gender", "Age"]}
SLICE_BASE_FIELDS = ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"]
SNAPSHOT_FIELDS = ["Name", "State", "Status", "DailyBudget", "ExcludedSites", "NegativeKeywords", "BiddingStrategy"]


def _num(v, cast=float):
    try:
        return cast(v) if v is not None else cast(0)
    except (TypeError, ValueError):
        return cast(0)


def _conversions(r: dict) -> int:
    """С целями Директ называет столбцы Conversions_<цель>_<модель атрибуции> — складываем все."""
    return sum(_num(v, int) for k, v in r.items() if k and k.startswith("Conversions"))


class DirectCollector(Collector):
    name = "direct"

    def __init__(self, client: DirectClient | None = None):
        self._client = client
        self.warning = ""

    @property
    def client(self) -> DirectClient:
        if self._client is None:
            s = get_settings()
            # Сбор статистики всегда идёт из боевого кабинета, кроме режима sandbox.
            self._client = DirectClient(s.direct_token, s.direct_client_login, sandbox=(s.direct_mode == "sandbox"))
        return self._client

    def configured(self) -> bool:
        return bool(self._client or get_settings().direct_token)

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        rows = self.client.report("CUSTOM_REPORT", AD_FIELDS, date_from, date_to)
        session.execute(delete(AdStat).where(AdStat.date.between(date_from, date_to), AdStat.platform == "direct"))
        for r in rows:
            network = r.get("AdNetworkType") or "SEARCH"
            session.add(
                AdStat(
                    date=date.fromisoformat(r["Date"]),
                    platform="direct",
                    campaign_id=_num(r.get("CampaignId"), int),
                    campaign_name=r.get("CampaignName") or "",
                    ad_group_id=_num(r.get("AdGroupId"), int) or None,
                    ad_group_name=r.get("AdGroupName"),
                    network=network,
                    placement=(r.get("Placement") or ("search" if network == "SEARCH" else "unknown")).lower(),
                    impressions=_num(r.get("Impressions"), int),
                    clicks=_num(r.get("Clicks"), int),
                    cost=_num(r.get("Cost")),
                )
            )
        n = len(rows)

        goals = get_settings().goal_ids
        q_rows = self._query_report(date_from, date_to, goals)
        session.execute(delete(SearchQuery).where(SearchQuery.date.between(date_from, date_to)))
        for r in q_rows:
            session.add(
                SearchQuery(
                    date=date.fromisoformat(r["Date"]),
                    campaign_id=_num(r.get("CampaignId"), int),
                    campaign_name=r.get("CampaignName") or "",
                    ad_group_id=_num(r.get("AdGroupId"), int) or None,
                    query=r.get("Query") or "",
                    impressions=_num(r.get("Impressions"), int),
                    clicks=_num(r.get("Clicks"), int),
                    cost=_num(r.get("Cost")),
                    criterion_type=r.get("CriterionType"),
                    criterion=r.get("Criterion"),
                    targeting_category=r.get("TargetingCategory"),
                    conversions=_conversions(r) if goals else None,
                )
            )
        n += len(q_rows)
        # Срезы для корректировок — тоже отдельно: без них работают все остальные инструменты.
        try:
            with session.begin_nested():
                n += self.collect_slices(session, date_from, date_to, goals)
        except Exception as e:
            self.warning = f"срезы для корректировок не сохранены: {type(e).__name__}: {e}"
        # Снимок настроек — отдельной точкой сохранения: его сбой не должен откатить статистику.
        try:
            with session.begin_nested():
                n += self.snapshot(session, date_to)
        except Exception as e:
            self.warning = f"снимок настроек не сохранён: {type(e).__name__}: {e}"
        return n

    def _query_report(self, date_from: date, date_to: date, goals: list[int]) -> list[dict]:
        conv = ["Conversions"] if goals else []
        attempts = [(QUERY_FIELDS + QUERY_EXTRA_FIELDS + conv, goals), (QUERY_FIELDS + conv, goals)]
        if conv:
            attempts.append((QUERY_FIELDS, []))
        for i, (fields, g) in enumerate(attempts):
            try:
                return self.client.report("SEARCH_QUERY_PERFORMANCE_REPORT", fields, date_from, date_to, goals=g or None)
            except DirectError:
                if i == len(attempts) - 1:
                    raise
        return []

    def collect_slices(self, session: Session, date_from: date, date_to: date, goals: list[int]) -> int:
        session.execute(delete(DirectSliceStat).where(DirectSliceStat.date.between(date_from, date_to)))
        n = 0
        for slice_name, slice_fields in SLICE_REPORTS.items():
            fields = SLICE_BASE_FIELDS + slice_fields + (["Conversions"] if goals else [])
            rows = self.client.report("CUSTOM_REPORT", fields, date_from, date_to, goals=goals or None)
            for r in rows:
                value = "|".join(str(r.get(f) or "UNKNOWN") for f in slice_fields)
                session.add(
                    DirectSliceStat(
                        date=date.fromisoformat(r["Date"]),
                        campaign_id=_num(r.get("CampaignId"), int),
                        campaign_name=r.get("CampaignName") or "",
                        slice=slice_name,
                        value=value,
                        impressions=_num(r.get("Impressions"), int),
                        clicks=_num(r.get("Clicks"), int),
                        cost=_num(r.get("Cost")),
                        conversions=_conversions(r),
                    )
                )
            n += len(rows)
        return n

    # ------------------------------------------------- снимок и разница ---
    def snapshot(self, session: Session, day: date) -> int:
        campaigns = self.client.get_campaigns()
        session.execute(delete(CampaignSnapshot).where(CampaignSnapshot.date == day))
        for c in campaigns:
            data = snapshot_data(c)
            session.add(CampaignSnapshot(date=day, campaign_id=int(c["Id"]), campaign_name=c.get("Name", ""), data=data))
        session.flush()
        record_changes(session, day)
        return len(campaigns)


def snapshot_data(c: dict) -> dict:
    # Текстово-графические кампании и Единая перфоманс-кампания (ЕПК) хранят стратегию в разных полях.
    strategy = (c.get("TextCampaign") or c.get("UnifiedCampaign") or {}).get("BiddingStrategy")
    budget = c.get("DailyBudget") or {}
    return {
        "Name": c.get("Name"),
        "Type": c.get("Type"),
        "State": c.get("State"),
        "Status": c.get("Status"),
        "DailyBudget": budget.get("Amount") / 1_000_000 if budget.get("Amount") else None,
        "ExcludedSites": sorted(items(c.get("ExcludedSites"))),
        "NegativeKeywords": sorted(items(c.get("NegativeKeywords"))),
        "BiddingStrategy": strategy,
    }


def diff_snapshots(before: dict, after: dict) -> list[tuple[str, str, str]]:
    """Сравнивает два снимка кампании → [(поле, было, стало)]. Для списков — добавлено/убрано."""
    changes = []
    for key in SNAPSHOT_FIELDS:
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if isinstance(b, list) or isinstance(a, list):
            added = sorted(set(a or []) - set(b or []))
            removed = sorted(set(b or []) - set(a or []))
            if added:
                changes.append((key, "", "добавлено: " + ", ".join(added)))
            if removed:
                changes.append((key, "убрано: " + ", ".join(removed), ""))
        else:
            changes.append((key, json.dumps(b, ensure_ascii=False), json.dumps(a, ensure_ascii=False)))
    return changes


def record_changes(session: Session, day: date) -> int:
    """Пишет в журнал изменения настроек относительно предыдущего снимка.

    Правки, которые сделала сама платформа, помечаются author=platform, остальные — external.
    """
    from ..rules.state import clean_word, platform_targets  # локальный импорт, чтобы избежать цикла

    prev_day = session.query(CampaignSnapshot.date).filter(CampaignSnapshot.date < day).order_by(CampaignSnapshot.date.desc()).first()
    if not prev_day:
        return 0
    prev = {s.campaign_id: s for s in session.query(CampaignSnapshot).filter(CampaignSnapshot.date == prev_day[0])}
    ours = platform_targets(session)
    session.execute(delete(SettingsChange).where(SettingsChange.date == day))
    n = 0
    for cur in session.query(CampaignSnapshot).filter(CampaignSnapshot.date == day):
        before = prev.get(cur.campaign_id)
        if not before:
            session.add(
                SettingsChange(date=day, campaign_id=cur.campaign_id, campaign_name=cur.campaign_name, field="Кампания", before=None, after="новая кампания")
            )
            n += 1
            continue
        for field, b, a in diff_snapshots(before.data or {}, cur.data or {}):
            words = [w.strip() for w in (a or b).split(":", 1)[-1].split(",")] if field in ("ExcludedSites", "NegativeKeywords") else []
            author = "platform" if words and all((int(cur.campaign_id), clean_word(w)) in ours for w in words) else "external"
            session.add(
                SettingsChange(
                    date=day, campaign_id=cur.campaign_id, campaign_name=cur.campaign_name, field=field, before=b, after=a, author=author
                )
            )
            n += 1
    return n
