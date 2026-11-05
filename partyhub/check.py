"""Проверка ключей: `python -m partyhub check`. Каждый источник делает один лёгкий запрос и ничего не пишет в базу."""
from __future__ import annotations

from datetime import timedelta

from .config import get_settings
from .pipeline.normalize import today_local, yesterday


def _metrika() -> str:
    from .collectors.metrika import MetrikaClient

    s = get_settings()
    if not (s.metrika_token and s.metrika_counter_id):
        return "skip:нет METRIKA_TOKEN или METRIKA_COUNTER_ID"
    rows = MetrikaClient(s.metrika_token, s.metrika_counter_id).stat(yesterday(), yesterday(), ["ym:s:UTMSource"], ["ym:s:visits"])
    goal = "" if s.metrika_goal_id else "; METRIKA_GOAL_ID не задан — брони не уйдут в Метрику"
    goals = f"; целей в консоли: {len(s.site_goal_ids)}"
    return f"ok:вчера {sum(int(float(r['metrics'][0] or 0)) for r in rows)} визитов{goals}{goal}"


def _callibri() -> str:
    from .collectors.callibri import CallibriClient, parse_statistics

    s = get_settings()
    if not (s.callibri_email and s.callibri_token and s.callibri_site_id):
        return "skip:нет CALLIBRI_EMAIL, CALLIBRI_TOKEN или CALLIBRI_SITE_ID"
    d = yesterday()
    payloads = CallibriClient(s.callibri_email, s.callibri_token).statistics(s.callibri_site_id, d - timedelta(days=6), d)
    touches = [t for p in payloads for t in parse_statistics(p)]
    with_utm = sum(1 for t in touches if t.utm_source)
    return f"ok:за 7 дней {len(touches)} обращений, с UTM-метками {with_utm}"


def _bitrix() -> str:
    from .collectors.bitrix import BitrixClient

    s = get_settings()
    if not s.bitrix_webhook_url:
        return "skip:нет BITRIX_WEBHOOK_URL"
    c = BitrixClient(s.bitrix_webhook_url)
    who = c.call("profile", {}).get("result") or {}
    total = c.call("crm.lead.list", {"select": ["ID"], "order": {"ID": "DESC"}}).get("total")
    return f"ok:вебхук от {who.get('NAME', '')} {who.get('LAST_NAME', '')}".rstrip() + f", лидов в CRM {total}"


def _bitrix_chat() -> str:
    from .collectors.bitrix import BitrixClient
    from .collectors.bitrix_chat import dialog_id

    s = get_settings()
    if not s.bitrix_chat_id:
        return "skip:нет BITRIX_CHAT_ID (список чатов: python -m partyhub bitrix-chats)"
    res = BitrixClient(s.bitrix_webhook_url).call("im.dialog.messages.get", {"DIALOG_ID": dialog_id(s.bitrix_chat_id), "LIMIT": 5})
    n = len((res.get("result") or {}).get("messages") or [])
    return f"ok:чат читается, последних сообщений получено {n}"


def _telegram() -> str:
    from .reports.telegram import Telegram

    s = get_settings()
    if not s.telegram_bot_token:
        return "skip:нет TELEGRAM_BOT_TOKEN"
    tg = Telegram()
    me = tg.call("getMe", {})
    parts = [f"бот @{me.get('username')}"]
    if not me.get("can_read_all_group_messages"):
        parts.append("режим приватности включён — в группе бот увидит только команды и ответы ему (@BotFather → /setprivacy → Disable)")
    if s.telegram_leads_chat_id:
        chat = tg.call("getChat", {"chat_id": s.telegram_leads_chat_id})
        parts.append(f"чат заявок «{chat.get('title') or chat.get('username')}»")
    else:
        parts.append("TELEGRAM_LEADS_CHAT_ID не задан (узнать: python -m partyhub telegram-chats)")
    hook = tg.call("getWebhookInfo", {}).get("url")
    parts.append(f"вебхук {hook}" if hook else "вебхука нет — запустите telegram-poll")
    return "ok:" + "; ".join(parts)


def _sheets() -> str:
    from . import sheets

    s = get_settings()
    if not sheets.configured():
        return "skip:нет GOOGLE_SHEET_ID или GOOGLE_SERVICE_ACCOUNT_FILE"
    tabs = sheets.client_from_settings().tabs()
    note = "" if s.google_sheet_tab in tabs else f"; листа «{s.google_sheet_tab}» нет — создам при первой выгрузке"
    return f"ok:таблица открывается, листов {len(tabs)}{note}"


def _topvisor() -> str:
    from .collectors.topvisor import TopvisorClient, region_names, split_ids

    s = get_settings()
    if not (s.topvisor_user_id and s.topvisor_api_key):
        return "skip:нет TOPVISOR_USER_ID или TOPVISOR_API_KEY"
    projects = TopvisorClient(s.topvisor_user_id, s.topvisor_api_key).projects()
    ids = {str(p.get("id")) for p in projects}
    wanted = split_ids(s.topvisor_project_id)
    if not wanted:
        return f"ok:ключ работает, проектов {len(projects)}; TOPVISOR_PROJECT_ID не задан (список: python -m partyhub topvisor-projects)"
    missing = [p for p in wanted if p not in ids]
    if missing:
        return f"error:проектов {', '.join(missing)} нет в Топвизоре (список: python -m partyhub topvisor-projects)"
    names = region_names(projects)
    regions = split_ids(s.topvisor_region_index)
    bad = [i for i in regions if not any((p, i) in names for p in wanted)]
    if bad:
        return f"error:регионов {', '.join(bad)} нет в выбранных проектах"
    n = len(regions) if regions else sum(1 for (p, _) in names if p in wanted)
    return f"ok:проектов {len(wanted)}, регионов {n}" + ("" if regions else " (все регионы проектов)")


def _yclients() -> str:
    from .collectors.yclients import YclientsClient

    s = get_settings()
    if not (s.yclients_partner_token and s.yclients_user_token and s.yclients_company_id):
        return "skip:нет токенов YCLIENTS или ID филиала"
    c = YclientsClient(s.yclients_partner_token, s.yclients_user_token)
    company = c.company(s.yclients_company_id)
    d = yesterday()
    n = len(c.records(s.yclients_company_id, d - timedelta(days=6), d))
    return f"ok:филиал «{company.get('title', s.yclients_company_id)}», записей за 7 дней {n}"


def _direct() -> str:
    from .collectors.direct import client_from_settings

    s = get_settings()
    if not s.direct_token:
        return "skip:нет токена Директа"
    where = "песочница" if s.direct_mode == "sandbox" else "боевой кабинет (только чтение)"
    campaigns = client_from_settings("sandbox" if s.direct_mode == "sandbox" else "live").get_campaigns()
    return f"ok:{where}: кампаний {len(campaigns)}; режим правок {s.direct_mode}"


CHECKS = {"Метрика": _metrika, "Callibri": _callibri, "Битрикс24": _bitrix, "Чат Битрикс24": _bitrix_chat,
          "Telegram": _telegram, "Google-таблица": _sheets, "Топвизор": _topvisor,
          "YCLIENTS": _yclients}
# для кнопок «Проверить» на экране «Подключения»
CHECK_BY_ID = {"direct": _direct, "metrika": _metrika, "callibri": _callibri, "bitrix": _bitrix, "telegram": _telegram,
               "sheets": _sheets, "topvisor": _topvisor, "yclients": _yclients}


def run_checks() -> list[tuple[str, str, str]]:
    out = []
    for name, fn in CHECKS.items():
        try:
            status, _, msg = fn().partition(":")
        except Exception as e:
            status, msg = "error", f"{type(e).__name__}: {e}"
        out.append((name, status, msg))
    return out


def print_checks() -> None:
    marks = {"ok": "✓", "skip": "·", "error": "✗"}
    print(f"Проверка источников ({today_local():%d.%m.%Y})")
    for name, status, msg in run_checks():
        print(f"{marks.get(status, '?')} {name}: {msg}")
