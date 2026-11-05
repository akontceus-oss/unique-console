"""ИИ-аудит кабинета: модель получает обезличенные выводы других инструментов и предлагает правки.

Каждая рекомендация проверяется платформой: кампания и площадка должны существовать в данных,
корректировка — в допустимом диапазоне, целевая цена — в пределах шага. Прошедшие проверку уходят
в журнал только на одобрение: ИИ никогда не меняет кабинет сам.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from ...ai.providers import LLMError, get_provider
from ...config import load_rules
from ...models import DirectSliceStat
from ...rules.engine import Proposal
from ..common import latest_snapshots, week_tag
from ..kinds import MODIFIER_RANGE, slice_label, strategy_cpa
from ..registry import TOOLS, Col, DirectTool, ToolContext, ToolResult, register

SYSTEM = (
    "Ты — специалист по Яндекс Директу. Реклама детского развлекательного центра (дни рождения, картинг) в Армении; "
    "главная цель — брони, главная метрика — цена брони. Тебе дают обезличенные данные кабинета за период: кампании, "
    "площадки РСЯ, поисковые запросы, автотаргетинг, срезы по устройствам и демографии, целевые цены. "
    "Предложи до 15 конкретных правок, опираясь только на переданные цифры. Не выдумывай кампании, площадки и запросы. "
    "Ответ — JSON-объект: {\"summary\": \"2–4 предложения о главном\", \"recommendations\": [{\"type\": "
    "\"exclude_site|add_negative|bid_modifier|set_cpa|note\", \"campaign_id\": число или null, \"target\": "
    "\"площадка, минус-фраза, группа среза (как в данных, поле value) или пусто\", \"value\": число или null "
    "(для bid_modifier — коэффициент в процентах 50–150, где 100 — без изменений; для set_cpa — новая целевая цена), "
    "\"reason\": \"почему, с цифрами\", \"priority\": \"high|medium|low\"}]}. "
    "Для идей, которые нельзя выразить правкой (тексты, посадочная, структура кампаний), используй type=note."
)
TYPE_RU = {"exclude_site": "Исключить площадку", "add_negative": "Минус-фраза", "bid_modifier": "Корректировка",
           "set_cpa": "Целевая цена", "note": "Идея"}
PRIORITY = {"high": ["Важно", "bad"], "medium": ["Средне", "warn"], "low": ["Можно позже", "muted"]}
FACT_TOOLS = ("conversions", "placements", "search_queries", "autotargeting", "bids", "modifiers")


def build_facts(ctx: ToolContext) -> dict:
    """Выводы инструментов без персональных данных: только агрегаты по кампаниям, площадкам, запросам и срезам."""
    facts = {"period": f"{ctx.d1.isoformat()}..{ctx.d2.isoformat()}", "targets": load_rules()["economics"]}
    for tid in FACT_TOOLS:
        tool = TOOLS.get(tid)
        if not tool or not tool.enabled():
            continue
        res = tool.run(ToolContext(ctx.session, ctx.d1, ctx.d2, {}))
        keys = [c.key for c in tool.columns]
        rows = []
        for r in res.rows[:40]:
            row = {k: (r[k][0] if isinstance(r.get(k), list) else r.get(k)) for k in keys}
            if "campaign_id" in r:
                row["campaign_id"] = r["campaign_id"]
            rows.append(row)
        facts[tid] = {"summary": {s["label"]: s["value"] for s in res.summary}, "rows": rows, "notes": res.notes}
    facts["slices_raw"] = [  # точные значения групп для bid_modifier
        {"campaign_id": int(cid), "slice": sl, "value": val}
        for cid, sl, val in ctx.session.execute(
            select(DirectSliceStat.campaign_id, DirectSliceStat.slice, DirectSliceStat.value)
            .where(DirectSliceStat.date.between(ctx.d1, ctx.d2)).distinct()
        ).all()
        if "UNKNOWN" not in val
    ]
    return facts


@register
class AIReview(DirectTool):
    id = "ai_review"
    title = "ИИ-аудит"
    group = "ИИ"
    order = 90
    uses_ai = True
    description = (
        "Нейросеть (GPT, Gemini, Claude, YandexGPT — выбирается сверху) смотрит на выводы всех инструментов и "
        "предлагает правки и идеи. Платформа проверяет каждую рекомендацию; прошедшие проверку можно отправить "
        "в журнал на одобрение. Модели уходят только обезличенные цифры."
    )
    columns = [
        Col("priority", "Приоритет", "chip"),
        Col("type", "Что сделать"),
        Col("campaign", "Кампания", wide=True),
        Col("target", "Объект"),
        Col("value", "Значение"),
        Col("reason", "Почему", "long"),
        Col("check", "Проверка", "chip"),
    ]

    def run(self, ctx: ToolContext) -> ToolResult:
        provider = get_provider(ctx.params.get("provider"))
        if provider.id == "template":
            return ToolResult(notes=["Выберите нейросеть сверху. Ключи GPT, Gemini, Claude или YandexGPT задаются в .env."])
        if not provider.configured():
            return ToolResult(notes=[f"Провайдер «{provider.title}» не настроен: задайте ключ в .env."])
        facts = build_facts(ctx)
        try:
            answer = provider.complete_json(SYSTEM, "Данные кабинета (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str),
                                            max_tokens=int(self.config().get("max_tokens", 6000)))
        except LLMError as e:
            return ToolResult(notes=[f"ИИ недоступен: {e}"])
        recs = answer.get("recommendations", []) if isinstance(answer, dict) else []
        rows, proposals = [], []
        known = _known(facts)
        known["strategy"] = {cid: strategy_cpa(d.get("BiddingStrategy")) for cid, d in latest_snapshots(ctx.session).items()}
        tag = week_tag(ctx.d2)
        for rec in recs[:30]:
            if not isinstance(rec, dict):
                continue
            ok, why, prop = _validate(rec, known, provider.title, tag)
            cid = rec.get("campaign_id")
            rows.append({
                "priority": PRIORITY.get(str(rec.get("priority")), PRIORITY["low"]),
                "type": TYPE_RU.get(rec.get("type"), str(rec.get("type"))),
                "campaign": known["names"].get(_int(cid), "—"), "campaign_id": _int(cid),
                "target": str(rec.get("target") or "—")[:200], "value": rec.get("value") if rec.get("value") is not None else "—",
                "reason": str(rec.get("reason") or "")[:600],
                "check": ["Можно в журнал", "good"] if prop else (["Идея", "muted"] if ok else [why, "bad"]),
            })
            if prop:
                proposals.append(prop)
        notes = [f"{provider.title} ({provider.model}): {answer.get('summary', '')}".strip()] if isinstance(answer, dict) else []
        return ToolResult(rows=rows, proposals=proposals, notes=notes, summary=[
            {"label": "Рекомендаций", "value": len(rows), "fmt": "int"},
            {"label": "Прошли проверку", "value": len(proposals), "fmt": "int", "tone": "good" if proposals else ""},
        ])


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _known(facts: dict) -> dict:
    names, placements, slices = {}, {}, set()
    for tid in FACT_TOOLS:
        for r in (facts.get(tid) or {}).get("rows", []):
            if r.get("campaign_id"):
                names[int(r["campaign_id"])] = r.get("campaign") or names.get(int(r["campaign_id"]))
    for r in (facts.get("conversions") or {}).get("rows", []):
        names[int(r["campaign_id"])] = r["campaign"]
    for r in (facts.get("placements") or {}).get("rows", []):
        placements[r["placement"]] = True
    for s in facts.get("slices_raw", []):
        slices.add((int(s["campaign_id"]), s["value"], s["slice"]))
    return {"names": names, "placements": placements, "slices": slices}


def _validate(rec: dict, known: dict, who: str, tag: str):
    """(можно ли показать, причина отказа, Proposal или None)."""
    t = rec.get("type")
    cid = _int(rec.get("campaign_id"))
    target = str(rec.get("target") or "").strip().lower()
    reason = f"ИИ ({who}): {str(rec.get('reason') or '').strip()[:500]}"
    if t == "note":
        return True, "", None
    if t not in TYPE_RU:
        return False, "Неизвестный тип", None
    if cid not in known["names"]:
        return False, "Нет такой кампании", None
    name = str(known["names"][cid])
    if t == "exclude_site":
        if target not in known["placements"]:
            return False, "Нет такой площадки", None
        return True, "", Proposal("ai_review", "approval", "exclude_site", target, reason, [cid], [name], {"source": "ai"})
    if t == "add_negative":
        if not target or len(target.split()) > 7:
            return False, "Минус-фраза пустая или длиннее 7 слов", None
        return True, "", Proposal("ai_review", "approval", "add_negative", target, reason, [cid], [name], {"source": "ai"})
    value = rec.get("value")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False, "Нет значения", None
    if t == "bid_modifier":
        match = [s for s in known["slices"] if s[0] == cid and s[1].lower() == target]
        if not match:
            return False, "Нет такой группы в срезах", None
        if not MODIFIER_RANGE[0] <= value <= MODIFIER_RANGE[1]:
            return False, "Корректировка вне 50–150%", None
        _, val, sl = match[0]
        mod = int(round(value / 5) * 5)
        return True, "", Proposal("ai_review", "approval", "bid_modifier", f"{slice_label(sl, val)} · {tag}", reason, [cid], [name],
                                  {"slice": sl, "value": val, "modifier": mod, "source": "ai"})
    if t == "set_cpa":
        cur = (known["strategy"].get(cid) or {})
        cur = next(iter(cur.values()), None)
        step = float((load_rules().get("direct_tools") or {}).get("bids", {}).get("max_step", 0.2))
        if not cur:
            return False, "В кампании нет целевой цены", None
        if abs(value / cur - 1) > step + 1e-9:
            return False, f"Шаг больше {step * 100:.0f}%", None
        old = known["strategy"].get(cid) or {}
        new = {side: float(round(v * value / cur)) for side, v in old.items()}
        return True, "", Proposal("ai_review", "approval", "set_cpa", f"Целевая цена · {tag}", reason, [cid], [name],
                                  {"new_cpa": new, "old_cpa": old, "source": "ai"})
    return False, "Неизвестный тип", None

