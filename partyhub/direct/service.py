"""Запуск инструментов Директа для API, утреннего цикла и командной строки."""
from __future__ import annotations

import json
import threading
import time
from datetime import date

from sqlalchemy.orm import Session

from ..ai.providers import LLMError, get_provider, list_providers
from ..config import get_settings
from ..rules.engine import save_proposals
from .common import campaign_types
from .registry import CAMPAIGN_TYPES, KINDS, ToolContext, ToolResult, all_tools, get_tool

# Ответ модели дорогой и каждый раз разный: «Отправить в журнал» берёт тот, что человек видел на экране.
_AI_CACHE: dict[tuple, tuple[float, ToolResult]] = {}
_AI_TTL = 3600
_lock = threading.Lock()


def manifest() -> dict:
    s = get_settings()
    default = (s.direct_ai_provider or s.ai_provider or "template").lower()
    return {
        "tools": [t.manifest() for t in all_tools()],
        "campaign_types": CAMPAIGN_TYPES,
        "providers": list_providers(),
        "default_provider": default if default != "template" else "",
        "mode": s.direct_mode,
        "currency": s.currency_symbol,
    }


def _run(session: Session, tool_id: str, d1: date, d2: date, params: dict, fresh: bool = True) -> ToolResult:
    tool = get_tool(tool_id)
    ctx = ToolContext(session, d1, d2, {**params, "_cfg": tool.config()})
    if not tool.uses_ai:
        return tool.run(ctx)
    key = (tool_id, (params.get("provider") or "").lower(), d1, d2)
    with _lock:
        hit = _AI_CACHE.get(key)
    if hit and (not fresh or time.time() - hit[0] < 60) and time.time() - hit[0] < _AI_TTL:
        return hit[1]
    if not fresh:
        raise ValueError("Сначала запустите ИИ-аудит — в журнал уходят рекомендации, которые вы видели")
    res = tool.run(ctx)
    with _lock:
        _AI_CACHE[key] = (time.time(), res)
    return res


def run_tool(session: Session, tool_id: str, d1: date, d2: date, params: dict | None = None) -> dict:
    res = _run(session, tool_id, d1, d2, params or {})
    types = campaign_types(session, d1, d2)
    rows = []
    for r in res.rows:
        cids = r.get("campaign_ids") or ([r["campaign_id"]] if r.get("campaign_id") else [])
        rows.append({**r, "_types": sorted({t for c in cids for t in types.get(int(c), [])})})
    return {
        "tool": get_tool(tool_id).manifest(),
        "period": {"from": d1.isoformat(), "to": d2.isoformat()},
        "rows": rows,
        "summary": res.summary,
        "notes": res.notes,
        "proposals": len([p for p in res.proposals if p.kind in KINDS]),
    }


def propose(session: Session, tool_id: str, d1: date, d2: date, params: dict | None = None, at=None,
            level: str | None = None) -> dict:
    """Сохраняет предложения инструмента в журнал. Всё, кроме сигналов, — только на одобрение человеком."""
    res = _run(session, tool_id, d1, d2, params or {}, fresh=False)
    props = []
    for p in res.proposals:
        if p.kind not in KINDS:
            continue
        p.level = "alert" if p.kind == "alert" else (level or "approval")
        props.append(p)
    saved = save_proposals(session, props, at)
    return {"saved": len(saved), "skipped": len(props) - len(saved), "ids": [a.id for a in saved]}


def daily_tools(session: Session, day: date, at=None, lookback: int = 30) -> dict:
    """Утренний цикл: инструменты с daily: true в rules.yaml кладут предложения в журнал на одобрение."""
    from datetime import timedelta

    d1 = day - timedelta(days=lookback - 1)
    out = {}
    for tool in all_tools():
        if not tool.config().get("daily"):
            continue
        if tool.uses_ai and not get_provider().configured():
            out[tool.id] = "пропущено: нейросеть не подключена"
            continue
        try:
            if tool.uses_ai:
                _run(session, tool.id, d1, day, {})
            out[tool.id] = propose(session, tool.id, d1, day, {}, at=at)["saved"]
            session.commit()
        except Exception as e:  # один инструмент не должен ронять утренний цикл
            session.rollback()
            out[tool.id] = f"ошибка: {type(e).__name__}: {e}"
    return out


ASK_SYSTEM = (
    "Ты — специалист по Яндекс Директу и помогаешь владельцу детского развлекательного центра в Армении. "
    "Отвечай по-русски, коротко и по делу, опираясь на переданные данные кабинета (обезличенные агрегаты). "
    "Если данных для ответа не хватает — так и скажи и подскажи, что посмотреть. Не выдумывай цифры."
)


def ask(session: Session, question: str, d1: date, d2: date, provider: str | None = None) -> dict:
    from .tools.ai_review import build_facts

    p = get_provider(provider)
    if not p.configured():
        raise LLMError(f"Провайдер «{p.title}» не настроен: задайте ключ в .env")
    facts = build_facts(ToolContext(session, d1, d2, {}))
    user = f"Данные кабинета (JSON):\n{json.dumps(facts, ensure_ascii=False, default=str)}\n\nВопрос: {question.strip()[:2000]}"
    return {"provider": p.title, "model": p.model, "answer": p.complete(ASK_SYSTEM, user, max_tokens=2000)}
