"""Операции плана изменений: что агент может предложить и как это применяется и откатывается.

Каждая операция:
  • schema   — параметры (JSON Schema), по ним агент вызывает функцию propose_<op>;
  • validate — проверка до того, как операция попадёт в план (ошибки возвращаются агенту, он их исправляет);
  • describe — понятное человеку описание для экрана «План изменений»;
  • apply    — выполнение в Директе, возвращает (результат, данные для отката);
  • rollback — возврат к прежнему состоянию.

Лимиты — в config/rules.yaml → direct_agent. Новая операция — новый класс с @op.
"""
from __future__ import annotations

import re
from datetime import date

from ..config import load_rules
from .api import AD_LIMITS, SITELINK_LIMITS, DirectAPI, items, micros

OPS: dict[str, "Op"] = {}
STATE_SERVICES = {"campaign": "campaigns", "ad": "ads", "keyword": "keywords"}


def limits() -> dict:
    return {
        "max_daily_budget": 50000, "max_bid": 3000, "max_ops": 30, "max_steps": 25, "max_campaigns": 5,
        "campaign_type": "UNIFIED_CAMPAIGN", "allow_moderation": True, "protected_campaign_ids": [],
        **(load_rules().get("direct_agent") or {}),
    }


def op(cls):
    OPS[cls.name] = cls()
    return cls


def _money(v) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


# --------------------------------------------------------------- схемы ---
KEYWORD = {"type": "object", "properties": {
    "phrase": {"type": "string", "description": "Ключевая фраза, до 7 слов; операторы Директа !, +, \"\", [] и минус-слова через -"},
    "bid": {"type": "number", "description": "Ставка на поиске (только для ручной стратегии)"}}, "required": ["phrase"]}
AD = {"type": "object", "properties": {
    "title": {"type": "string", "description": f"Заголовок, до {AD_LIMITS['title']} символов"},
    "title2": {"type": "string", "description": f"Второй заголовок, до {AD_LIMITS['title2']} символов"},
    "text": {"type": "string", "description": f"Текст, до {AD_LIMITS['text']} символов"},
    "href": {"type": "string", "description": "Ссылка на сайт с UTM-метками"},
    "display_path": {"type": "string", "description": "Отображаемая ссылка, до 20 символов"}},
    "required": ["title", "text", "href"]}
SITELINK = {"type": "object", "properties": {"title": {"type": "string"}, "href": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title", "href"]}
GROUP = {"type": "object", "properties": {
    "name": {"type": "string"},
    "region_ids": {"type": "array", "items": {"type": "integer"}, "description": "id регионов из find_regions"},
    "keywords": {"type": "array", "items": KEYWORD},
    "ads": {"type": "array", "items": AD},
    "negative_keywords": {"type": "array", "items": {"type": "string"}}},
    "required": ["name", "region_ids", "ads"]}


def check_ad(a: dict, where: str) -> list[str]:
    errs = []
    for f, n in AD_LIMITS.items():
        v = a.get(f) or ""
        if len(v) > n:
            errs.append(f"{where}: поле {f} длиннее {n} символов ({len(v)}): «{v}»")
    if not str(a.get("href", "")).startswith(("http://", "https://")):
        errs.append(f"{where}: ссылка должна начинаться с https://")
    if not a.get("title") or not a.get("text"):
        errs.append(f"{where}: нужны заголовок и текст")
    return errs


def check_keywords(kws: list[dict], where: str, lim: dict) -> list[str]:
    errs = []
    for k in kws:
        words = [w for w in re.split(r"\s+", str(k.get("phrase", "")).split(" -")[0].strip()) if w]
        if not words or len(words) > 7:
            errs.append(f"{where}: фраза «{k.get('phrase')}» — нужно от 1 до 7 слов")
        if k.get("bid") and float(k["bid"]) > lim["max_bid"]:
            errs.append(f"{where}: ставка {k['bid']} выше лимита {lim['max_bid']}")
    return errs


def check_protected(cid, lim) -> list[str]:
    return [f"кампания {cid} защищена от изменений (protected_campaign_ids)"] if int(cid) in {int(x) for x in lim["protected_campaign_ids"]} else []


class Op:
    name = ""
    title = ""
    description = ""
    schema: dict = {}

    def validate(self, p: dict, lim: dict) -> list[str]:
        return []

    def describe(self, p: dict) -> str:
        return self.title

    def apply(self, api: DirectAPI, p: dict, today: date) -> tuple[dict, dict]:
        raise NotImplementedError

    def rollback(self, api: DirectAPI, p: dict, result: dict, undo: dict) -> None:
        pass


# ============================================================ операции ===
@op
class CreateCampaign(Op):
    name = "create_campaign"
    title = "Создать кампанию"
    description = ("Новая кампания целиком: стратегия, бюджет, минус-слова, группы с регионами, ключевыми фразами и объявлениями, "
                   "быстрые ссылки. Создаётся черновиком: на модерацию не уходит и не показывается, пока не вызвать moderate.")
    schema = {"type": "object", "properties": {
        "name": {"type": "string"},
        "daily_budget": {"type": "number", "description": "Дневной бюджет в валюте кабинета; для автостратегий — недельный = ×7"},
        "networks": {"type": "string", "enum": ["search", "network", "both"], "description": "search — поиск, network — РСЯ"},
        "strategy": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["manual", "max_clicks", "average_cpa"]},
            "cpa": {"type": "number", "description": "Целевая цена конверсии для average_cpa"},
            "goal_id": {"type": "integer", "description": "Цель Метрики для average_cpa"},
            "weekly_budget": {"type": "number"}}, "required": ["type"]},
        "negative_keywords": {"type": "array", "items": {"type": "string"}},
        "excluded_sites": {"type": "array", "items": {"type": "string"}},
        "sitelinks": {"type": "array", "items": SITELINK, "description": "До 8 быстрых ссылок"},
        "groups": {"type": "array", "items": GROUP},
        "submit_for_moderation": {"type": "boolean", "description": "Сразу отправить объявления на модерацию (по умолчанию нет — черновик)"}},
        "required": ["name", "daily_budget", "networks", "strategy", "groups"]}

    def validate(self, p, lim):
        errs = []
        if float(p.get("daily_budget") or 0) <= 0:
            errs.append("нужен дневной бюджет")
        if float(p.get("daily_budget") or 0) > lim["max_daily_budget"]:
            errs.append(f"дневной бюджет {p['daily_budget']} выше лимита {lim['max_daily_budget']}")
        st = p.get("strategy") or {}
        if st.get("type") == "average_cpa" and not (st.get("cpa") and st.get("goal_id")):
            errs.append("для average_cpa нужны cpa и goal_id")
        if st.get("type") == "manual" and p.get("networks") == "network":
            errs.append("ручная стратегия только в РСЯ не поддерживается — выберите max_clicks или average_cpa")
        if not p.get("groups"):
            errs.append("нужна хотя бы одна группа")
        if p.get("submit_for_moderation") and not lim["allow_moderation"]:
            errs.append("отправка на модерацию выключена в rules.yaml (direct_agent.allow_moderation)")
        for i, g in enumerate(p.get("groups") or [], 1):
            where = f"группа {i} «{g.get('name')}»"
            if not g.get("region_ids"):
                errs.append(f"{where}: нужны регионы (find_regions)")
            if not g.get("ads"):
                errs.append(f"{where}: нужно хотя бы одно объявление")
            if p.get("networks") != "network" and not g.get("keywords"):
                errs.append(f"{where}: для поиска нужны ключевые фразы")
            errs += check_keywords(g.get("keywords") or [], where, lim)
            for j, a in enumerate(g.get("ads") or [], 1):
                errs += check_ad(a, f"{where}, объявление {j}")
        links = p.get("sitelinks") or []
        if len(links) > 8:
            errs.append("быстрых ссылок не больше 8")
        for l in links:
            if len(l.get("title", "")) > SITELINK_LIMITS["title"] or len(l.get("description") or "") > SITELINK_LIMITS["description"]:
                errs.append(f"быстрая ссылка «{l.get('title')}» длиннее допустимого (30 / 60)")
        return errs

    def describe(self, p):
        st = p.get("strategy") or {}
        strategy = {"manual": "ручные ставки", "max_clicks": "максимум кликов", "average_cpa": f"цена конверсии {_money(st.get('cpa') or 0)}"}
        nets = {"search": "поиск", "network": "РСЯ", "both": "поиск и РСЯ"}
        lines = [f"Создать кампанию «{p['name']}» ({nets.get(p.get('networks'), p.get('networks'))}), "
                 f"{strategy.get(st.get('type'), st.get('type'))}, бюджет {_money(p['daily_budget'])} в день — "
                 + ("и отправить на модерацию" if p.get("submit_for_moderation") else "черновиком")]
        if p.get("negative_keywords"):
            lines.append("Минус-слова: " + ", ".join(p["negative_keywords"]))
        for g in p.get("groups") or []:
            lines.append(f"Группа «{g['name']}», регионы {g.get('region_ids')}: "
                         + f"ключевых фраз — {len(g.get('keywords') or [])} ({', '.join(k['phrase'] for k in (g.get('keywords') or [])[:6])}"
                         + ("…" if len(g.get("keywords") or []) > 6 else "") + ")")
            for a in g.get("ads") or []:
                lines.append(f"  Объявление: {a['title']}" + (f" / {a['title2']}" if a.get("title2") else "") + f" — {a['text']} → {a['href']}")
        if p.get("sitelinks"):
            lines.append("Быстрые ссылки: " + ", ".join(l["title"] for l in p["sitelinks"]))
        return "\n".join(lines)

    def apply(self, api, p, today):
        lim = limits()
        spec = {**p, "campaign_type": lim["campaign_type"]}
        res: dict = {"groups": []}
        undo: dict = {}
        if p.get("sitelinks"):
            res["sitelink_set_id"] = api.add_sitelinks(p["sitelinks"])
        res["campaign_id"] = undo["campaign_id"] = api.add_campaign(spec, today)
        for g in p["groups"]:
            gid = api.add_adgroup(res["campaign_id"], g)
            kids = api.add_keywords(gid, g["keywords"]) if g.get("keywords") else []
            aids = api.add_ads(gid, g["ads"], res.get("sitelink_set_id"))
            res["groups"].append({"adgroup_id": gid, "keyword_ids": kids, "ad_ids": aids})
        if p.get("submit_for_moderation"):
            api.state("ads", "moderate", [a for g in res["groups"] for a in g["ad_ids"]])
            res["moderation"] = True
        return res, undo

    def rollback(self, api, p, result, undo):
        cid = undo.get("campaign_id") or result.get("campaign_id")
        if cid:
            try:
                api.state("campaigns", "delete", [cid])  # удаляется только кампания, которая ни разу не показывалась
            except Exception:
                api.state("campaigns", "suspend", [cid])


@op
class UpdateCampaign(Op):
    name = "update_campaign"
    title = "Изменить кампанию"
    description = "Название, дневной бюджет (для ручной стратегии), минус-слова и исключённые площадки кампании."
    schema = {"type": "object", "properties": {
        "campaign_id": {"type": "integer"},
        "name": {"type": "string"},
        "daily_budget": {"type": "number"},
        "negative_keywords_add": {"type": "array", "items": {"type": "string"}},
        "negative_keywords_remove": {"type": "array", "items": {"type": "string"}},
        "excluded_sites_add": {"type": "array", "items": {"type": "string"}},
        "excluded_sites_remove": {"type": "array", "items": {"type": "string"}}},
        "required": ["campaign_id"]}

    def validate(self, p, lim):
        errs = check_protected(p["campaign_id"], lim)
        if p.get("daily_budget") and float(p["daily_budget"]) > lim["max_daily_budget"]:
            errs.append(f"дневной бюджет {p['daily_budget']} выше лимита {lim['max_daily_budget']}")
        if not any(p.get(k) for k in self.schema["properties"] if k != "campaign_id"):
            errs.append("нечего менять")
        return errs

    def describe(self, p):
        parts = []
        if p.get("name"):
            parts.append(f"название → «{p['name']}»")
        if p.get("daily_budget"):
            parts.append(f"бюджет → {_money(p['daily_budget'])} в день")
        for key, label in (("negative_keywords_add", "+ минус-слова"), ("negative_keywords_remove", "− минус-слова"),
                           ("excluded_sites_add", "+ исключить площадки"), ("excluded_sites_remove", "− вернуть площадки")):
            if p.get(key):
                parts.append(f"{label}: {', '.join(p[key])}")
        return f"Кампания {p['campaign_id']}: " + "; ".join(parts)

    def apply(self, api, p, today):
        c = api.campaign_full(int(p["campaign_id"]))
        undo = {"Name": c.get("Name"), "DailyBudget": c.get("DailyBudget"),
                "NegativeKeywords": items(c.get("NegativeKeywords")), "ExcludedSites": items(c.get("ExcludedSites"))}
        fields: dict = {}
        if p.get("name"):
            fields["Name"] = p["name"]
        if p.get("daily_budget"):
            fields["DailyBudget"] = {"Amount": micros(p["daily_budget"]), "Mode": (c.get("DailyBudget") or {}).get("Mode", "STANDARD")}
        for field, add, rm in (("NegativeKeywords", "negative_keywords_add", "negative_keywords_remove"),
                               ("ExcludedSites", "excluded_sites_add", "excluded_sites_remove")):
            if p.get(add) or p.get(rm):
                cur = undo[field]
                drop = {x.lower() for x in p.get(rm) or []}
                new = [x for x in cur if x.lower() not in drop] + [x for x in p.get(add) or [] if x.lower() not in {c.lower() for c in cur}]
                fields[field] = {"Items": sorted(set(new))} if new else None
        api.update_campaign(int(p["campaign_id"]), fields)
        return {"changed": sorted(fields)}, {k: undo[k] for k in fields}

    def rollback(self, api, p, result, undo):
        fields = {}
        for k, v in undo.items():
            if k in ("NegativeKeywords", "ExcludedSites"):
                fields[k] = {"Items": v} if v else None
            elif v is not None:
                fields[k] = v
        if fields:
            api.update_campaign(int(p["campaign_id"]), fields)


@op
class SetState(Op):
    name = "set_state"
    title = "Остановить или запустить"
    description = "Остановить (suspend) или запустить (resume) кампании, объявления или ключевые фразы."
    schema = {"type": "object", "properties": {
        "target": {"type": "string", "enum": ["campaign", "ad", "keyword"]},
        "ids": {"type": "array", "items": {"type": "integer"}},
        "action": {"type": "string", "enum": ["suspend", "resume"]}},
        "required": ["target", "ids", "action"]}

    def validate(self, p, lim):
        errs = [] if p.get("ids") else ["нужны id"]
        if p.get("target") == "campaign":
            for cid in p.get("ids") or []:
                errs += check_protected(cid, lim)
        return errs

    def describe(self, p):
        what = {"campaign": "кампании", "ad": "объявления", "keyword": "ключевые фразы"}[p["target"]]
        return f"{'Остановить' if p['action'] == 'suspend' else 'Запустить'} {what}: {', '.join(map(str, p['ids']))}"

    def apply(self, api, p, today):
        api.state(STATE_SERVICES[p["target"]], p["action"], p["ids"])
        return {"ids": p["ids"]}, {"action": "resume" if p["action"] == "suspend" else "suspend"}

    def rollback(self, api, p, result, undo):
        api.state(STATE_SERVICES[p["target"]], undo["action"], p["ids"])


@op
class AddKeywords(Op):
    name = "add_keywords"
    title = "Добавить ключевые фразы"
    description = "Новые ключевые фразы в существующую группу."
    schema = {"type": "object", "properties": {"adgroup_id": {"type": "integer"}, "keywords": {"type": "array", "items": KEYWORD}},
              "required": ["adgroup_id", "keywords"]}

    def validate(self, p, lim):
        return check_keywords(p.get("keywords") or [], f"группа {p.get('adgroup_id')}", lim) or ([] if p.get("keywords") else ["нет фраз"])

    def describe(self, p):
        return f"Группа {p['adgroup_id']}: добавить фразы — " + ", ".join(k["phrase"] for k in p["keywords"])

    def apply(self, api, p, today):
        ids = api.add_keywords(int(p["adgroup_id"]), p["keywords"])
        return {"keyword_ids": ids}, {"keyword_ids": ids}

    def rollback(self, api, p, result, undo):
        if undo.get("keyword_ids"):
            api.state("keywords", "delete", undo["keyword_ids"])


@op
class AddAds(Op):
    name = "add_ads"
    title = "Добавить объявления"
    description = "Новые текстовые объявления в существующую группу (черновиками)."
    schema = {"type": "object", "properties": {"adgroup_id": {"type": "integer"}, "ads": {"type": "array", "items": AD}},
              "required": ["adgroup_id", "ads"]}

    def validate(self, p, lim):
        errs = [] if p.get("ads") else ["нет объявлений"]
        for j, a in enumerate(p.get("ads") or [], 1):
            errs += check_ad(a, f"объявление {j}")
        return errs

    def describe(self, p):
        return f"Группа {p['adgroup_id']}: добавить объявления — " + "; ".join(f"{a['title']} — {a['text']}" for a in p["ads"])

    def apply(self, api, p, today):
        ids = api.add_ads(int(p["adgroup_id"]), p["ads"])
        return {"ad_ids": ids}, {"ad_ids": ids}

    def rollback(self, api, p, result, undo):
        if undo.get("ad_ids"):
            api.state("ads", "delete", undo["ad_ids"])


@op
class SetKeywordBids(Op):
    name = "set_keyword_bids"
    title = "Ставки на ключевые фразы"
    description = "Ставки на поиске для ключевых фраз (кампании с ручной стратегией)."
    schema = {"type": "object", "properties": {"bids": {"type": "array", "items": {"type": "object", "properties": {
        "keyword_id": {"type": "integer"}, "bid": {"type": "number"}}, "required": ["keyword_id", "bid"]}}}, "required": ["bids"]}

    def validate(self, p, lim):
        errs = [] if p.get("bids") else ["нет ставок"]
        for b in p.get("bids") or []:
            if float(b["bid"]) <= 0 or float(b["bid"]) > lim["max_bid"]:
                errs.append(f"ставка {b['bid']} для фразы {b['keyword_id']} вне 0…{lim['max_bid']}")
        return errs

    def describe(self, p):
        return "Ставки: " + ", ".join(f"фраза {b['keyword_id']} → {_money(b['bid'])}" for b in p["bids"])

    def apply(self, api, p, today):
        new = {int(b["keyword_id"]): float(b["bid"]) for b in p["bids"]}
        before = api.keyword_bids(list(new))
        api.set_keyword_bids(new)
        return {"set": len(new)}, {"bids": {str(k): v for k, v in before.items() if v}}

    def rollback(self, api, p, result, undo):
        if undo.get("bids"):
            api.set_keyword_bids({int(k): v for k, v in undo["bids"].items()})


@op
class Moderate(Op):
    name = "moderate"
    title = "Отправить на модерацию"
    description = "Отправить черновые объявления кампании на модерацию. После одобрения кампания начнёт показываться."
    schema = {"type": "object", "properties": {"ad_ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ad_ids"]}

    def validate(self, p, lim):
        if not lim["allow_moderation"]:
            return ["отправка на модерацию выключена в rules.yaml (direct_agent.allow_moderation)"]
        return [] if p.get("ad_ids") else ["нужны id объявлений"]

    def describe(self, p):
        return f"Отправить на модерацию объявления: {', '.join(map(str, p['ad_ids']))} — после одобрения начнутся показы"

    def apply(self, api, p, today):
        api.state("ads", "moderate", p["ad_ids"])
        return {"ad_ids": p["ad_ids"]}, {"ad_ids": p["ad_ids"]}

    def rollback(self, api, p, result, undo):
        api.state("ads", "suspend", undo["ad_ids"])  # модерацию не отменить — объявления останавливаются
