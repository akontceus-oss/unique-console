"""Командная строка платформы: python -m partyhub <команда>."""
from __future__ import annotations

import argparse
import json
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"Нужен Python 3.11 или новее, сейчас {sys.version.split()[0]}. Запустите через python3.11+ или venv.")
from datetime import date, timedelta

from .config import get_settings
from .db import create_all, init_engine, session_scope


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="partyhub", description="Платформа маркетинга детских праздников")
    p.add_argument("--project", "-p", help="проект (адрес из списка: python -m partyhub projects); без него — общая база из .env")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="создать таблицы")
    pr = sub.add_parser("projects", help="список проектов, создание: projects create «Crazy Drift», "
                        "архив: projects archive|restore <адрес>, удалить просроченные: projects purge")
    pr.add_argument("action", nargs="?", choices=["list", "create", "archive", "restore", "purge"], default="list")
    pr.add_argument("name", nargs="?", help="название (create) или адрес проекта (archive, restore)")
    pr.add_argument("--slug", help="адрес проекта в ссылке, по умолчанию из названия")
    pr.add_argument("--from-env", action="store_true", help="перенести в проект подключения, заполненные в .env")

    d = sub.add_parser("demo", help="заполнить базу демо-данными (стирает текущие данные!)")
    d.add_argument("--end", help="последний день с данными, YYYY-MM-DD (по умолчанию вчера)")
    d.add_argument("--days", type=int, default=90)
    d.add_argument("--yes", action="store_true", help="подтвердить очистку базы")

    c = sub.add_parser("collect", help="забрать данные из источников")
    c.add_argument("--from", dest="date_from")
    c.add_argument("--to", dest="date_to")
    c.add_argument("--only", help="direct,metrika,callibri,bitrix24,bitrix_chat,topvisor")

    sub.add_parser("rebuild-leads", help="пересобрать заявки из обращений и CRM")

    r = sub.add_parser("rules", help="прогнать правила за день")
    r.add_argument("--day")

    dl = sub.add_parser("daily", help="утренний цикл: сбор, заявки, правила, оценка правок")
    dl.add_argument("--day")

    rp = sub.add_parser("report", help="собрать ежедневный отчёт")
    rp.add_argument("--day")
    rp.add_argument("--print", action="store_true", help="только напечатать, не отправлять")

    ex = sub.add_parser("export", help="выгрузить показатели в JSON")
    ex.add_argument("--out", default="export")
    ex.add_argument("--days", type=int, default=30)

    dt = sub.add_parser("direct", help="инструменты вкладки «Директ»: список, запуск, предложения в журнал")
    dt.add_argument("tool", nargs="?", help="id инструмента; без него — список")
    dt.add_argument("--days", type=int, default=30)
    dt.add_argument("--provider", default="", help="провайдер ИИ для ai_review: openai, gemini, anthropic, yandexgpt")
    dt.add_argument("--propose", action="store_true", help="сохранить предложения в журнал (на одобрение)")

    sub.add_parser("upload-conversions", help="отправить брони в Метрику как офлайн-конверсии")
    sub.add_parser("direct-check", help="проверить токен Директа: список кампаний")
    tg = sub.add_parser("telegram-webhook", help="привязать бота к адресу платформы")
    tg.add_argument("--url", help="публичный адрес платформы, по умолчанию PUBLIC_BASE_URL")
    tp = sub.add_parser("telegram-poll", help="читать Telegram без публичного адреса: заявки из чата и кнопки отчёта")
    tp.add_argument("--drop-webhook", action="store_true", help="снять вебхук бота перед опросом")
    sub.add_parser("telegram-chats", help="показать чаты, где бот недавно видел сообщения (для TELEGRAM_LEADS_CHAT_ID)")
    sub.add_parser("bitrix-chats", help="показать последние чаты Битрикс24 (для BITRIX_CHAT_ID)")
    sub.add_parser("topvisor-projects", help="показать проекты и регионы Топвизора (для TOPVISOR_PROJECT_ID и TOPVISOR_REGION_INDEX)")
    sub.add_parser("leads-sync", help="свежие заявки: Callibri, Битрикс24, чат → склейка → Google-таблица")
    sh = sub.add_parser("sheets-sync", help="выгрузить заявки в Google-таблицу")
    sh.add_argument("--from", dest="date_from", help="с какой даты, по умолчанию rules.yaml → sheets.days_back")
    sub.add_parser("check", help="проверить ключи Метрики, Callibri, Битрикс24, Telegram, Google-таблицы и Топвизора")
    sv = sub.add_parser("serve", help="запустить веб-интерфейс и API")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("scheduler", help="планировщик: сбор и отчёт по расписанию")

    a = p.parse_args(argv)
    if a.project:
        from .projects import use_project

        try:
            with use_project(a.project):
                return _run(a)
        except KeyError as e:
            raise SystemExit(str(e).strip("'\""))
    init_engine()
    create_all()
    _run(a)


def _run(a: argparse.Namespace) -> None:
    create_all()

    if a.cmd == "projects":
        from . import projects as P

        if a.action == "create":
            if not a.name:
                raise SystemExit("Укажите название: python -m partyhub projects create «Crazy Drift»")
            values = None
            if a.from_env:
                base = get_settings().model_dump()
                values = {k: str(base[k]) for k in P.FIELDS if k in base and str(base[k] or "") and k not in ("business_name",)}
            try:
                proj = P.create_project(a.name, a.slug, values)
            except ValueError as e:
                raise SystemExit(str(e))
            print(f"Проект создан: {proj.name} → /p/{proj.slug}/" + (f" (перенесено полей: {len(values)})" if values else ""))
        elif a.action in ("archive", "restore"):
            if not a.name:
                raise SystemExit(f"Укажите адрес проекта: python -m partyhub projects {a.action} crazy-drift")
            try:
                proj = (P.archive_project if a.action == "archive" else P.restore_project)(a.name)
            except KeyError:
                raise SystemExit(f"Нет проекта «{a.name}»")
            print(f"{proj.name}: " + (f"в архиве, удалится {proj.purge_at:%d.%m.%Y}" if proj.archived_at else "снова активен"))
        elif a.action == "purge":
            print("Удалены:", ", ".join(P.purge_expired()) or "нечего удалять")
        for proj in P.list_projects(archived=None):
            print(f"{proj.slug}\t{proj.name}" + (f"\tархив, удалится {proj.purge_at:%d.%m.%Y}" if proj.archived_at else ""))
    elif a.cmd == "init-db":
        print("Таблицы созданы:", get_settings().database_url.split("@")[-1])
    elif a.cmd == "demo":
        if not a.yes:
            raise SystemExit("Команда стирает все данные в базе. Повторите с --yes.")
        from .demo import build_demo
        from .pipeline.normalize import yesterday

        with session_scope() as s:
            build_demo(s, end=_date(a.end) or yesterday(), days=a.days)
        print("Демо-данные готовы")
    elif a.cmd == "collect":
        from .jobs import collect

        res = collect(None, _date(a.date_from), _date(a.date_to), a.only.split(",") if a.only else None)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.cmd == "rebuild-leads":
        from .pipeline.merge import rebuild_leads

        with session_scope() as s:
            print("Заявок:", rebuild_leads(s))
    elif a.cmd == "rules":
        from .rules.actions import ActionApplier
        from .rules.engine import run_rules

        with session_scope() as s:
            print(json.dumps(run_rules(s, ActionApplier(s), day=_date(a.day)), ensure_ascii=False))
    elif a.cmd == "daily":
        from .jobs import daily

        print(json.dumps(daily(_date(a.day)), ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "report":
        from .reports.daily import build_report, send_report

        with session_scope() as s:
            if a.print:
                text, ids = build_report(s, _date(a.day))
                print(text)
                print("\nКнопки для правок:", ids)
            else:
                log = send_report(s, _date(a.day))
                print("Отправлено" if log.delivered else f"Не отправлено: {log.error}")
    elif a.cmd == "export":
        from .export import export_json
        from .pipeline.metrics import period

        with session_scope() as s:
            d1, d2 = period(a.days)
            for path in export_json(s, d1, d2, a.out):
                print(path)
    elif a.cmd == "direct":
        from .direct import service
        from .pipeline.metrics import period

        if not a.tool:
            for t in service.manifest()["tools"]:
                print(f"{t['id']:16} {t['group']:10} {t['title']}")
            return
        d1, d2 = period(a.days)
        with session_scope() as s:
            res = service.run_tool(s, a.tool, d1, d2, {"provider": a.provider})
            for item in res["summary"]:
                print(f"{item['label']}: {item['value']}")
            for note in res["notes"]:
                print("•", note)
            print(f"Строк: {len(res['rows'])}, предложений: {res['proposals']}")
            if a.propose:
                print(json.dumps(service.propose(s, a.tool, d1, d2, {"provider": a.provider}), ensure_ascii=False))
    elif a.cmd == "upload-conversions":
        from .collectors.metrika import upload_conversions

        with session_scope() as s:
            print(upload_conversions(s))
    elif a.cmd == "direct-check":
        from .collectors.direct import client_from_settings

        st = get_settings()
        client = client_from_settings("sandbox" if st.direct_mode == "sandbox" else "live")
        for c in client.get_campaigns():
            print(c["Id"], c.get("State"), c.get("Name"))
    elif a.cmd == "telegram-webhook":
        from .reports.telegram import Telegram

        st = get_settings()
        base = (a.url or st.public_base_url).rstrip("/")
        if not base:
            raise SystemExit("Укажите --url или PUBLIC_BASE_URL")
        print(Telegram().set_webhook(f"{base}/api/telegram", st.telegram_webhook_secret))
    elif a.cmd == "telegram-poll":
        from .chats.telegram_in import poll_forever
        from .reports.telegram import Telegram
        from .web.app import rebuilder

        if a.drop_webhook:
            Telegram().call("deleteWebhook", {})
        poll_forever(on_saved=rebuilder.schedule)
    elif a.cmd == "telegram-chats":
        from .chats.telegram_in import chat_list_hint

        chats = chat_list_hint()
        if not chats:
            print("Пусто. Добавьте бота в чат, напишите там любое сообщение и повторите (вебхук должен быть снят).")
        for c in chats:
            print(c["id"], c["type"], c["title"])
    elif a.cmd == "bitrix-chats":
        from .collectors.bitrix import BitrixClient

        res = BitrixClient(get_settings().bitrix_webhook_url).call("im.recent.list", {}).get("result") or {}
        for item in res if isinstance(res, list) else res.get("items", []):
            if item.get("type") == "chat":
                print(item.get("id"), (item.get("chat") or {}).get("name") or item.get("title"))
    elif a.cmd == "topvisor-projects":
        from .collectors.topvisor import TopvisorClient

        s = get_settings()
        for pr in TopvisorClient(s.topvisor_user_id, s.topvisor_api_key).projects():
            print(f"{pr.get('id')}  {pr.get('name') or ''}  {pr.get('site') or ''}")
            for se in pr.get("searchers") or []:
                for r in se.get("regions") or []:
                    print(f"    регион {r.get('index')}  {r.get('name')} · {se.get('name')}")
    elif a.cmd == "leads-sync":
        from .jobs import leads_sync

        print(json.dumps(leads_sync(), ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "sheets-sync":
        from datetime import datetime

        from . import sheets

        since = datetime.combine(_date(a.date_from), datetime.min.time()) if a.date_from else None
        with session_scope() as s:
            print(sheets.sync(s, since=since))
    elif a.cmd == "check":
        from .check import print_checks

        print_checks()
    elif a.cmd == "serve":
        import uvicorn

        uvicorn.run("partyhub.web.app:app", host=a.host, port=a.port)
    elif a.cmd == "scheduler":
        from .jobs import scheduler_loop

        scheduler_loop()


if __name__ == "__main__":
    main()
