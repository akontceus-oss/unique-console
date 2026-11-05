"""Полный доступ к кабинету Директа для агента: чтение структуры, отчёты, создание и правка кампаний.

Обёртка над DirectClient.call. Деньги в API — в микроединицах валюты кабинета (×1 000 000),
здесь наружу отдаются обычные числа.

Документация: https://yandex.ru/dev/direct/doc/ru/ — сервисы campaigns, adgroups, ads, keywords,
keywordbids, sitelinks, dictionaries. Кампании, группы и объявления — через v501 (нужен для ЕПК),
остальное — через v5. Форматы создания ЕПК сверьте в песочнице перед боевым режимом.
"""
from __future__ import annotations

from datetime import date

from ..collectors.direct import SERVICE_VERSION, DirectClient, DirectError, items
from ..config import get_settings

SERVICE_VERSION.update({"adgroups": "v501", "ads": "v501", "keywords": "v5", "keywordbids": "v5", "sitelinks": "v5",
                        "dictionaries": "v5"})
MICRO = 1_000_000

# Ограничения Директа на текстовые объявления.
AD_LIMITS = {"title": 56, "title2": 30, "text": 81, "display_path": 20}
SITELINK_LIMITS = {"title": 30, "description": 60}


def micros(v) -> int:
    return int(round(float(v) * 100)) * (MICRO // 100)  # Директ принимает суммы с точностью до копеек


def money(v) -> float | None:
    return round(v / MICRO, 2) if isinstance(v, (int, float)) else None


def read_client() -> DirectClient:
    """Чтение — из боевого кабинета (в dry_run тоже: оно ничего не меняет), в sandbox — из песочницы."""
    s = get_settings()
    return DirectClient(s.direct_token, s.direct_client_login, sandbox=(s.direct_mode == "sandbox"))


def write_client(mode: str) -> DirectClient:
    s = get_settings()
    return DirectClient(s.direct_token, s.direct_client_login, sandbox=(mode != "live"))


def _results(res: dict, key: str) -> list[dict]:
    out = res.get(key) or []
    for r in out:
        if r.get("Errors"):
            e = r["Errors"][0]
            raise DirectError(e.get("Code"), e.get("Message"), e.get("Details", ""))
    return out


class DirectAPI:
    def __init__(self, client: DirectClient):
        self.c = client

    # ================================================================ чтение ===
    def campaigns(self) -> list[dict]:
        res = self.c.call("campaigns", "get", {
            "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED", "ENDED"]},
            "FieldNames": ["Id", "Name", "Type", "State", "Status", "DailyBudget", "StartDate"],
            "TextCampaignFieldNames": ["BiddingStrategy"],
            "UnifiedCampaignFieldNames": ["BiddingStrategy"],
        })
        out = []
        for c in res.get("Campaigns", []):
            strategy = (c.get("TextCampaign") or c.get("UnifiedCampaign") or {}).get("BiddingStrategy") or {}
            out.append({
                "id": c["Id"], "name": c.get("Name"), "type": c.get("Type"), "state": c.get("State"), "status": c.get("Status"),
                "daily_budget": money((c.get("DailyBudget") or {}).get("Amount")), "start_date": c.get("StartDate"),
                "strategy": {side: (strategy.get(side) or {}).get("BiddingStrategyType") for side in ("Search", "Network")},
            })
        return out

    def campaign_full(self, campaign_id: int) -> dict:
        res = self.c.call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [campaign_id]},
            "FieldNames": ["Id", "Name", "Type", "State", "Status", "DailyBudget", "NegativeKeywords", "ExcludedSites", "StartDate"],
            "TextCampaignFieldNames": ["BiddingStrategy", "Settings"],
            "UnifiedCampaignFieldNames": ["BiddingStrategy", "Settings"],
        })
        found = res.get("Campaigns") or []
        if not found:
            raise DirectError("not_found", f"кампания {campaign_id} не найдена")
        return found[0]

    def structure(self, campaign_id: int, limit: int = 200) -> dict:
        c = self.campaign_full(campaign_id)
        groups = self.c.call("adgroups", "get", {
            "SelectionCriteria": {"CampaignIds": [campaign_id]},
            "FieldNames": ["Id", "Name", "Status", "RegionIds", "NegativeKeywords"],
        }).get("AdGroups", [])
        kws = self.c.call("keywords", "get", {
            "SelectionCriteria": {"CampaignIds": [campaign_id]},
            "FieldNames": ["Id", "AdGroupId", "Keyword", "State", "Status", "Bid"],
        }).get("Keywords", [])
        ads = self.c.call("ads", "get", {
            "SelectionCriteria": {"CampaignIds": [campaign_id]},
            "FieldNames": ["Id", "AdGroupId", "State", "Status"],
            "TextAdFieldNames": ["Title", "Title2", "Text", "Href"],
        }).get("Ads", [])
        strategy = (c.get("TextCampaign") or c.get("UnifiedCampaign") or {}).get("BiddingStrategy")
        return {
            "campaign": {"id": c["Id"], "name": c.get("Name"), "type": c.get("Type"), "state": c.get("State"),
                         "status": c.get("Status"), "daily_budget": money((c.get("DailyBudget") or {}).get("Amount")),
                         "negative_keywords": items(c.get("NegativeKeywords")), "excluded_sites": items(c.get("ExcludedSites")),
                         "strategy": strategy},
            "groups": [{
                "id": g["Id"], "name": g.get("Name"), "status": g.get("Status"), "region_ids": g.get("RegionIds"),
                "negative_keywords": items(g.get("NegativeKeywords")),
                "keywords": [{"id": k["Id"], "phrase": k.get("Keyword"), "state": k.get("State"), "bid": money(k.get("Bid"))}
                             for k in kws if k.get("AdGroupId") == g["Id"]][:limit],
                "ads": [{"id": a["Id"], "state": a.get("State"), "status": a.get("Status"),
                         **{k.lower(): (a.get("TextAd") or {}).get(k) for k in ("Title", "Title2", "Text", "Href")}}
                        for a in ads if a.get("AdGroupId") == g["Id"]][:limit],
            } for g in groups],
        }

    _regions: list[dict] | None = None

    def find_regions(self, query: str, limit: int = 15) -> list[dict]:
        if DirectAPI._regions is None:
            res = self.c.call("dictionaries", "get", {"DictionaryNames": ["GeoRegions"]})
            DirectAPI._regions = res.get("GeoRegions", [])
        q = query.strip().lower()
        hits = [r for r in DirectAPI._regions if q in str(r.get("GeoRegionName", "")).lower()]
        hits.sort(key=lambda r: (str(r.get("GeoRegionName", "")).lower() != q, len(str(r.get("GeoRegionName", "")))))
        return [{"id": r["GeoRegionId"], "name": r.get("GeoRegionName"), "type": r.get("GeoRegionType"), "parent_id": r.get("ParentId")}
                for r in hits[:limit]]

    def report(self, date_from: date, date_to: date, fields: list[str], report_type: str = "CUSTOM_REPORT",
               campaign_ids: list[int] | None = None, goals: list[int] | None = None) -> list[dict]:
        filters = [{"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in campaign_ids]}] if campaign_ids else None
        return self.c.report(report_type, fields, date_from, date_to, goals=goals, filters=filters)

    # ================================================================ запись ===
    def add_sitelinks(self, links: list[dict]) -> int:
        res = self.c.call("sitelinks", "add", {"SitelinksSets": [{"Sitelinks": [
            {"Title": l["title"], "Href": l["href"], **({"Description": l["description"]} if l.get("description") else {})}
            for l in links]}]})
        return _results(res, "AddResults")[0]["Id"]

    def add_campaign(self, spec: dict, start: date) -> int:
        kind = spec.get("campaign_type") or "UNIFIED_CAMPAIGN"
        key = "UnifiedCampaign" if kind == "UNIFIED_CAMPAIGN" else "TextCampaign"
        camp = {"Name": spec["name"], "StartDate": start.isoformat(), key: {"BiddingStrategy": strategy_payload(spec)}}
        if spec.get("negative_keywords"):
            camp["NegativeKeywords"] = {"Items": spec["negative_keywords"]}
        if spec.get("excluded_sites"):
            camp["ExcludedSites"] = {"Items": spec["excluded_sites"]}
        if (spec.get("strategy") or {}).get("type", "manual") == "manual" and spec.get("daily_budget"):
            camp["DailyBudget"] = {"Amount": micros(spec["daily_budget"]), "Mode": "STANDARD"}
        res = self.c.call("campaigns", "add", {"Campaigns": [camp]})
        return _results(res, "AddResults")[0]["Id"]

    def add_adgroup(self, campaign_id: int, g: dict) -> int:
        group = {"Name": g["name"], "CampaignId": campaign_id, "RegionIds": [int(r) for r in g["region_ids"]]}
        if g.get("negative_keywords"):
            group["NegativeKeywords"] = {"Items": g["negative_keywords"]}
        return _results(self.c.call("adgroups", "add", {"AdGroups": [group]}), "AddResults")[0]["Id"]

    def add_keywords(self, adgroup_id: int, keywords: list[dict]) -> list[int]:
        kws = [{"AdGroupId": adgroup_id, "Keyword": k["phrase"], **({"Bid": micros(k["bid"])} if k.get("bid") else {})} for k in keywords]
        return [r["Id"] for r in _results(self.c.call("keywords", "add", {"Keywords": kws}), "AddResults")]

    def add_ads(self, adgroup_id: int, ads: list[dict], sitelink_set_id: int | None = None) -> list[int]:
        out = []
        for a in ads:
            ad = {"Title": a["title"], "Text": a["text"], "Href": a["href"], "Mobile": "NO"}
            if a.get("title2"):
                ad["Title2"] = a["title2"]
            if a.get("display_path"):
                ad["DisplayUrlPath"] = a["display_path"]
            if sitelink_set_id:
                ad["SitelinkSetId"] = sitelink_set_id
            out.append({"AdGroupId": adgroup_id, "TextAd": ad})
        return [r["Id"] for r in _results(self.c.call("ads", "add", {"Ads": out}), "AddResults")]

    def update_campaign(self, campaign_id: int, fields: dict) -> None:
        _results(self.c.call("campaigns", "update", {"Campaigns": [{"Id": campaign_id, **fields}]}), "UpdateResults")

    def state(self, service: str, action: str, ids: list[int]) -> None:
        """service: campaigns | ads | keywords; action: suspend | resume | delete | archive | moderate."""
        key = {"suspend": "SuspendResults", "resume": "ResumeResults", "delete": "DeleteResults", "archive": "ArchiveResults",
               "moderate": "ModerateResults"}[action]
        _results(self.c.call(service, action, {"SelectionCriteria": {"Ids": [int(i) for i in ids]}}), key)

    def keyword_bids(self, keyword_ids: list[int]) -> dict[int, float | None]:
        res = self.c.call("keywords", "get", {"SelectionCriteria": {"Ids": [int(i) for i in keyword_ids]}, "FieldNames": ["Id", "Bid"]})
        return {k["Id"]: money(k.get("Bid")) for k in res.get("Keywords", [])}

    def set_keyword_bids(self, bids: dict[int, float]) -> None:
        _results(self.c.call("keywordbids", "set", {"KeywordBids": [
            {"KeywordId": int(k), "SearchBid": micros(v)} for k, v in bids.items()]}), "SetResults")


def strategy_payload(spec: dict) -> dict:
    """Стратегия для новой кампании. type: manual | max_clicks | average_cpa; networks: search | network | both."""
    st = spec.get("strategy") or {}
    t = st.get("type", "manual")
    nets = spec.get("networks", "search")
    weekly = micros(st.get("weekly_budget") or (float(spec.get("daily_budget") or 0) * 7)) if t != "manual" else None
    if t == "manual":
        active = {"BiddingStrategyType": "HIGHEST_POSITION"}
    elif t == "max_clicks":
        active = {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {"WeeklySpendLimit": weekly}}
    elif t == "average_cpa":
        cpa = {"AverageCpa": micros(st["cpa"]), "GoalId": int(st["goal_id"]), "WeeklySpendLimit": weekly}
        active = {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": cpa}
    else:
        raise ValueError(f"неизвестная стратегия {t}")
    off = {"BiddingStrategyType": "SERVING_OFF"}
    if t == "manual":
        network = {"BiddingStrategyType": "MAXIMUM_COVERAGE"} if nets in ("network", "both") else off
    else:
        network = {"BiddingStrategyType": "NETWORK_DEFAULT"} if nets == "both" else (active if nets == "network" else off)
    return {"Search": active if nets in ("search", "both") else off, "Network": network}
