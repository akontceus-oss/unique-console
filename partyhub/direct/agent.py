"""ИИ-агент Директа: выполняет задание человека — смотрит кабинет, строит отчёты, готовит изменения.

Как это устроено:
  1. Человек пишет задание («создай поисковую кампанию на дни рождения, 5 000 в день, Ереван»).
  2. Модель (GPT, Gemini, Claude, YandexGPT — ai/providers.py) сама вызывает функции: читает кампании
     и статистику, ищет регионы, считает показатели из CRM, показывает таблицы.
  3. Любое изменение модель может только предложить (propose_*): оно проверяется по лимитам и попадает
     в план (DirectChangeset). Кабинет не меняется.
  4. Человек смотрит план и нажимает «Применить» — тогда изменения уходят в Директ (в режиме DIRECT_MODE),
     с записью результата и возможностью отката.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.providers import LLMError, get_provider
from ..config import get_settings, load_rules
from ..db import session_scope
from ..models import DirectAgentRun, DirectChangeset
from ..pipeline.normalize import now_local, yesterday
from ..rules.actions import direct_write_lock
from .api import DirectAPI, read_client, write_client
from .changeset import OPS, limits

MAX_RESULT_CHARS = 15000

REPORT_FIELDS_HINT = (
    "Частые поля: Date, CampaignId, CampaignName, AdGroupId, AdGroupName, Criterion, CriterionType, Query, AdNetworkType, Placement, "
    "Device, Gender, Age, LocationOfPresenceName, Impressions, Clicks, Cost, Ctr, AvgCpc, Conversions, ConversionRate, CostPerConversion, "
    "BounceRate, AvgPageviews. Типы: CUSTOM_REPORT, SEARCH_QUERY_PERFORMANCE_REPORT, CRITERIA_PERFORMANCE_REPORT, CAMPAIGN_PERFORMANCE_REPORT."
)


def _tools() -> list[dict]:
    read = [
        {"name": "list_campaigns", "description": "Все кампании кабинета: id, название, тип, состояние, бюджет, стратегия.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "get_campaign", "description": "Структура кампании: настройки, минус-слова, группы, ключевые фразы со ставками, объявления.",
         "parameters": {"type": "object", "properties": {"campaign_id": {"type": "integer"}}, "required": ["campaign_id"]}},
        {"name": "run_report", "description": "Отчёт Директа за период (сервис Reports). " + REPORT_FIELDS_HINT,
         "parameters": {"type": "object", "properties": {
             "date_from": {"type": "string", "description": "YYYY-MM-DD"}, "date_to": {"type": "string", "description": "YYYY-MM-DD"},
             "fields": {"type": "array", "items": {"type": "string"}},
             "report_type": {"type": "string"},
             "campaign_ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["date_from", "date_to", "fields"]}},
        {"name": "business_metrics", "description": "Заявки и брони из CRM по кампаниям Директа за N дней (главная метрика — цена брони), "
                                                    "и выводы инструментов платформы: площадки, запросы, автотаргетинг, конверсии, ставки, корректировки.",
         "parameters": {"type": "object", "properties": {"days": {"type": "integer"},
                                                         "tool": {"type": "string", "enum": ["conversions", "placements", "search_queries",
                                                                                            "autotargeting", "bids", "modifiers"]}},
                        "required": ["tool"]}},
        {"name": "find_regions", "description": "Найти id региона показа по названию (Ереван, Армения…).",
         "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "show_table", "description": "Показать человеку таблицу-отчёт в окне агента.",
         "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
                                                         "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
                        "required": ["title", "columns", "rows"]}},
    ]
    write = [{"name": f"propose_{o.name}", "description": f"{o.title}: {o.description} Только добавляет в план, кабинет не меняется.",
              "parameters": o.schema} for o in OPS.values()]
    return read + write


_COUNTRIES = {"AM": "в Армении", "RU": "в России", "KZ": "в Казахстане", "BY": "в Беларуси", "GE": "в Грузии"}


def _where(region: str) -> str:
    c = _COUNTRIES.get((region or "").strip().upper())
    return f", клиенты {c}" if c else ""


def system_prompt() -> str:
    s = get_settings()
    rules = load_rules()
    lim = limits()
    econ = rules["economics"]
    return (
        f"Ты — специалист по Яндекс Директу и управляешь кабинетом рекламодателя через функции. Бизнес: {s.business_name} "
        f"(детские праздники, картинг){_where(s.phone_region)}. Валюта кабинета: {s.currency}. Сегодня {now_local():%Y-%m-%d}. "
        f"Цели: цена заявки {econ['target_cpl']}, цена брони {econ['target_cpb']}, средний чек {econ['average_check']}. "
        "Главная метрика — цена брони из CRM (business_metrics), а не клики.\n"
        "Правила работы:\n"
        "— Сначала смотри данные (list_campaigns, get_campaign, run_report, business_metrics), потом предлагай изменения.\n"
        "— Менять кабинет напрямую ты не можешь: любые изменения — только через функции propose_*. Они попадают в план, "
        "который человек проверит и применит сам. Не говори, что что-то уже изменено.\n"
        "— Если функция вернула ошибки проверки, исправь параметры и вызови снова.\n"
        f"— Лимиты: дневной бюджет кампании до {lim['max_daily_budget']}, ставка до {lim['max_bid']}, не больше {lim['max_ops']} операций в плане. "
        "Новые кампании создаются черновиком; на модерацию — только если человек прямо просит.\n"
        "— Тексты объявлений: заголовок до 56 символов, второй заголовок до 30, текст до 81; без восклицательных знаков в заголовке, "
        "с выгодой и призывом. Ключевые фразы до 7 слов, без мусорных запросов; общие минус-слова (бесплатно, вакансии, скачать…) — в кампанию.\n"
        "— В ссылках используй метки: utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={source_type}|{source}"
        "&utm_term={keyword}" + (f". Сайт: {lim['site_url']}" if lim.get("site_url") else ". Адрес сайта спроси у человека, если его нет в задании") + ".\n"
        "— Регионы ищи через find_regions, не угадывай id.\n"
        "— Для отчёта покажи главное таблицей (show_table) и коротко объясни выводы.\n"
        "— Если задание неясно или не хватает данных (сайт, бюджет, цель Метрики), задай вопрос и закончи ход.\n"
        "Отвечай по-русски, коротко: что сделал, что в плане и что человеку проверить."
    )


# ================================================================ инструменты ===
class Toolbox:
    def __init__(self, session: Session, run: DirectAgentRun, api: DirectAPI | None = None):
        self.s = session
        self.run = run
        self._api = api

    @property
    def api(self) -> DirectAPI:
        if self._api is None:
            self._api = DirectAPI(read_client())
        return self._api

    def call(self, name: str, args: dict):
        try:
            if name.startswith("propose_"):
                return self.propose(name[len("propose_"):], args)
            fn = getattr(self, "t_" + name, None)
            if not fn:
                return {"error": f"нет функции {name}"}
            return fn(**args)
        except TypeError as e:
            return {"error": f"неверные параметры: {e}"}
        except Exception as e:  # ошибка API уходит модели: она может поправиться
            return {"error": f"{type(e).__name__}: {e}"}

    def t_list_campaigns(self):
        return {"campaigns": self.api.campaigns()}

    def t_get_campaign(self, campaign_id):
        return self.api.structure(int(campaign_id))

    def t_run_report(self, date_from, date_to, fields, report_type="CUSTOM_REPORT", campaign_ids=None):
        rows = self.api.report(date.fromisoformat(date_from), date.fromisoformat(date_to), list(fields), report_type or "CUSTOM_REPORT",
                               campaign_ids, get_settings().goal_ids or None)
        return {"rows": rows[:300], "total_rows": len(rows)}

    def t_business_metrics(self, tool, days=30):
        from .service import run_tool

        d2 = yesterday()
        res = run_tool(self.s, tool, d2 - timedelta(days=int(days) - 1), d2, {})
        keys = [c["key"] for c in res["tool"]["columns"]] + ["campaign_id"]
        rows = [{k: (r[k][0] if isinstance(r.get(k), list) else r.get(k)) for k in keys if k in r} for r in res["rows"][:60]]
        return {"period": res["period"], "summary": {x["label"]: x["value"] for x in res["summary"]}, "rows": rows, "notes": res["notes"]}

    def t_find_regions(self, query):
        return {"regions": self.api.find_regions(query)}

    def t_show_table(self, title, columns, rows):
        self.run.steps = self.run.steps + [{"type": "table", "title": str(title)[:200], "columns": [str(c) for c in columns][:12],
                                            "rows": [[str(v) for v in r][:12] for r in rows[:200]]}]
        return {"ok": True}

    def propose(self, name: str, args: dict):
        if name not in OPS:
            return {"error": f"нет операции {name}"}
        lim = limits()
        o = OPS[name]
        errs = o.validate(args, lim)
        if errs:
            return {"ok": False, "errors": errs}
        cs = self.changeset()
        if len(cs.ops) >= lim["max_ops"]:
            return {"ok": False, "errors": [f"в плане уже {len(cs.ops)} операций — это предел"]}
        cs.ops = cs.ops + [{"op": name, "params": args, "description": o.describe(args)}]
        return {"ok": True, "plan_id": cs.id, "operations_in_plan": len(cs.ops)}

    def changeset(self) -> DirectChangeset:
        cs = self.s.get(DirectChangeset, self.run.changeset_id) if self.run.changeset_id else None
        if cs is None or cs.status != "draft":
            cs = DirectChangeset(created_at=now_local(), run_id=self.run.id, title=self.run.task[:300], status="draft", ops=[])
            self.s.add(cs)
            self.s.flush()
            self.run.changeset_id = cs.id
        return cs


# ===================================================================== цикл ===
def _short(v, n=300) -> str:
    t = json.dumps(v, ensure_ascii=False, default=str)
    return t if len(t) <= n else t[:n] + "…"


def step(session: Session, run: DirectAgentRun, provider=None, api: DirectAPI | None = None) -> None:
    """Ведёт диалог, пока модель не ответит текстом или не кончится лимит шагов."""
    provider = provider or get_provider(run.provider)
    box = Toolbox(session, run, api)
    tools = _tools()
    max_steps = int(limits()["max_steps"])
    for _ in range(max_steps):
        out = provider.chat(system_prompt(), run.messages, tools, max_tokens=6000)
        msg = {"role": "assistant", "content": out["content"], "tool_calls": out["tool_calls"]}
        if out.get("raw"):
            msg["raw"] = out["raw"]
        run.messages = run.messages + [msg]
        if out["content"] and out["tool_calls"]:
            run.steps = run.steps + [{"type": "thought", "text": out["content"]}]
        if not out["tool_calls"]:
            run.answer = out["content"]
            run.steps = run.steps + [{"type": "answer", "text": out["content"]}]
            run.status = "done"
            break
        for c in out["tool_calls"]:
            res = box.call(c["name"], c["args"])
            text = json.dumps(res, ensure_ascii=False, default=str)
            if len(text) > MAX_RESULT_CHARS:
                text = text[:MAX_RESULT_CHARS] + '…" (обрезано: сузьте запрос)'
                text = json.dumps({"truncated": text})
            run.messages = run.messages + [{"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": text}]
            ok = not (isinstance(res, dict) and (res.get("error") or res.get("errors")))
            run.steps = run.steps + [{"type": "call", "name": c["name"], "args": _short(c["args"], 200), "ok": ok,
                                      "result": _short(res.get("error") or res.get("errors") if not ok else res, 200)}]
        run.updated_at = now_local()
        session.commit()
    else:
        run.status = "done"
        run.answer = "Остановился: слишком много шагов. Уточните задание или разбейте его на части."
        run.steps = run.steps + [{"type": "answer", "text": run.answer}]
    run.updated_at = now_local()
    session.commit()


def _work(run_id: int, provider=None, api=None) -> None:
    with session_scope() as s:
        run = s.get(DirectAgentRun, run_id)
        try:
            step(s, run, provider, api)
        except LLMError as e:
            s.rollback()
            run = s.get(DirectAgentRun, run_id)
            run.status, run.error = "error", str(e)
        except Exception as e:
            s.rollback()
            run = s.get(DirectAgentRun, run_id)
            run.status, run.error = "error", f"{type(e).__name__}: {e}"
            print(traceback.format_exc(), flush=True)


def start(task: str, provider: str, author: str = "", sync: bool = False, llm=None, api=None) -> int:
    p = llm or get_provider(provider)
    if not llm and not p.configured():
        raise LLMError(f"Провайдер «{p.title}» не настроен: задайте ключ в .env")
    with session_scope() as s:
        run = DirectAgentRun(created_at=now_local(), updated_at=now_local(), task=task.strip()[:4000], provider=provider,
                             status="running", messages=[{"role": "user", "content": task.strip()[:4000]}],
                             steps=[{"type": "task", "text": task.strip()[:4000]}], author=author)
        s.add(run)
        s.flush()
        run_id = run.id
    _launch(run_id, sync, llm, api)
    return run_id


def reply(run_id: int, text: str, sync: bool = False, llm=None, api=None) -> None:
    with session_scope() as s:
        run = s.get(DirectAgentRun, run_id)
        if not run:
            raise KeyError(run_id)
        if run.status == "running":
            raise ValueError("Агент ещё работает над прошлым сообщением")
        run.messages = run.messages + [{"role": "user", "content": text.strip()[:4000]}]
        run.steps = run.steps + [{"type": "task", "text": text.strip()[:4000]}]
        run.status, run.error = "running", None
    _launch(run_id, sync, llm, api)


def _launch(run_id, sync, llm, api):
    if sync:
        _work(run_id, llm, api)
    else:
        threading.Thread(target=_work, args=(run_id, llm, api), daemon=True).start()


# ============================================================ план изменений ===
def changeset_view(cs: DirectChangeset | None) -> dict | None:
    if not cs:
        return None
    return {"id": cs.id, "status": cs.status, "mode": cs.mode, "title": cs.title,
            "decided_by": cs.decided_by, "applied_at": cs.applied_at.isoformat(timespec="minutes") if cs.applied_at else None,
            "ops": [{"op": o["op"], "title": OPS[o["op"]].title if o["op"] in OPS else o["op"], "description": o["description"],
                     "result": o.get("result"), "error": o.get("error"), "rolled_back": o.get("rolled_back")} for o in cs.ops]}


def run_view(session: Session, run: DirectAgentRun) -> dict:
    return {"id": run.id, "task": run.task, "provider": run.provider, "status": run.status, "error": run.error,
            "created_at": run.created_at.isoformat(timespec="minutes"), "steps": run.steps, "answer": run.answer,
            "changeset": changeset_view(session.get(DirectChangeset, run.changeset_id) if run.changeset_id else None)}


def recent_runs(session: Session, limit: int = 15) -> list[dict]:
    rows = session.scalars(select(DirectAgentRun).order_by(DirectAgentRun.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "task": r.task[:200], "status": r.status, "created_at": r.created_at.isoformat(timespec="minutes"),
             "provider": r.provider} for r in rows]


class PlanError(RuntimeError):
    pass


def apply_changeset(session: Session, cs_id: int, by: str, api: DirectAPI | None = None, mode: str | None = None) -> DirectChangeset:
    cs = session.get(DirectChangeset, cs_id, with_for_update=True)
    if not cs:
        raise KeyError(cs_id)
    if cs.status != "draft":
        raise PlanError(f"План уже в статусе «{cs.status}»")
    if not cs.ops:
        raise PlanError("План пустой")
    lim = limits()
    for o in cs.ops:  # лимиты могли поменяться после составления плана
        errs = OPS[o["op"]].validate(o["params"], lim)
        if errs:
            raise PlanError(f"Операция «{o['description'][:80]}» не проходит проверку: {'; '.join(errs)}")
    mode = mode or get_settings().direct_mode
    cs.status, cs.mode, cs.decided_by, cs.decided_at = "applying", mode, by, now_local()
    session.commit()
    ops = [dict(o) for o in cs.ops]
    done = 0
    with direct_write_lock(session):
        if mode != "dry_run":
            api = api or DirectAPI(write_client(mode))
        for o in ops:
            if mode == "dry_run":
                o["result"] = {"dry_run": True}
                done += 1
                continue
            try:
                o["result"], o["undo"] = OPS[o["op"]].apply(api, o["params"], now_local().date())
                done += 1
            except Exception as e:
                o["error"] = f"{type(e).__name__}: {e}"[:1000]
                break
            finally:
                cs.ops = [dict(x) for x in ops]
                session.commit()
    cs.ops = ops
    cs.status = "applied" if done == len(ops) else ("partial" if done else "failed")
    cs.applied_at = now_local()
    session.commit()
    return cs


def reject_changeset(session: Session, cs_id: int, by: str) -> DirectChangeset:
    cs = session.get(DirectChangeset, cs_id, with_for_update=True)
    if not cs:
        raise KeyError(cs_id)
    if cs.status != "draft":
        raise PlanError(f"План уже в статусе «{cs.status}»")
    cs.status, cs.decided_by, cs.decided_at = "rejected", by, now_local()
    session.commit()
    return cs


def rollback_changeset(session: Session, cs_id: int, by: str, api: DirectAPI | None = None) -> DirectChangeset:
    cs = session.get(DirectChangeset, cs_id, with_for_update=True)
    if not cs:
        raise KeyError(cs_id)
    if cs.status not in ("applied", "partial"):
        raise PlanError("Откатить можно только применённый план")
    mode = get_settings().direct_mode
    if cs.mode != "dry_run" and cs.mode != mode:
        raise PlanError(f"План применён в режиме {cs.mode}, а платформа сейчас в режиме {mode}. Откатите вручную в кабинете.")
    ops = [dict(o) for o in cs.ops]
    errors = []
    with direct_write_lock(session):
        if cs.mode != "dry_run":
            api = api or DirectAPI(write_client(cs.mode))
        for o in reversed(ops):
            if "result" not in o or o.get("rolled_back"):
                continue
            try:
                if cs.mode != "dry_run":
                    OPS[o["op"]].rollback(api, o["params"], o["result"], o.get("undo") or {})
                o["rolled_back"] = True
            except Exception as e:
                errors.append(f"{o['description'][:60]}: {type(e).__name__}: {e}")
    cs.ops = ops
    if errors:
        session.commit()
        raise PlanError("Откат не завершён: " + "; ".join(errors))
    cs.status, cs.rolled_back_at = "rolled_back", now_local()
    cs.decided_by = f"{cs.decided_by or ''}; откат: {by}".strip("; ")
    session.commit()
    return cs
