"""Заявки из чатов (Telegram, чат Битрикс24) и выгрузка в Google-таблицу."""
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from partyhub.chats.parse import parse_message
from partyhub.chats.store import save_chat_message
from partyhub.collectors.bitrix import BitrixClient
from partyhub.collectors.bitrix_chat import BitrixChatCollector, clean_text
from partyhub.models import Touch

TG = {"X-Telegram-Bot-Api-Secret-Token": "tgsecret"}


# ------------------------------------------------------------ разбор текста
def test_parse_template_message():
    p = parse_message("Новая заявка\n*Имя:* Анна\nТелефон: 091 12-34-56\nutm_source: yandex\nutm_campaign=712345\n"
                      "Комментарий: ДР на 10 детей")
    assert p.phone == "+37491123456" and p.name == "Анна"
    assert p.utm == {"utm_source": "yandex", "utm_campaign": "712345"}
    assert p.comment == "ДР на 10 детей" and p.looks_like_lead


def test_parse_free_text_skips_dates_and_reads_url_utm():
    p = parse_message("Анна, день рождения 25.10.2026 на 12 детей, звоните +374 (77) 55-44-33 "
                      "https://site.am/?utm_source=yandex&utm_medium=cpc&utm_content=context|games.am")
    assert p.phone == "+37477554433"
    assert p.utm["utm_medium"] == "cpc" and p.utm["utm_content"] == "context|games.am"
    assert p.name is None and "Анна" in p.comment


def test_plain_chatter_is_not_a_lead(session):
    assert not parse_message("ок, перезвонил, ждём предоплату").looks_like_lead
    assert save_chat_message(session, "telegram", "tg:1:1", datetime(2026, 10, 20, 10), "ок, перезвонил") is None
    t = save_chat_message(session, "telegram", "tg:1:2", datetime(2026, 10, 20, 10), "Имя: Карен\nСообщение: хотим в субботу")
    assert t is not None and t.phone is None and t.name == "Карен"


def test_edited_message_updates_touch(session):
    save_chat_message(session, "telegram", "tg:1:5", datetime(2026, 10, 20, 10), "Анна 091123456")
    save_chat_message(session, "telegram", "tg:1:5", datetime(2026, 10, 20, 10), "Анна 077554433")
    rows = session.query(Touch).filter_by(external_id="tg:1:5").all()
    assert len(rows) == 1 and rows[0].phone == "+37477554433"


# ------------------------------------------------------------ Telegram
@pytest.fixture
def client(fixed_now, monkeypatch):
    monkeypatch.setenv("TELEGRAM_LEADS_CHAT_ID", "-100777")
    from partyhub import config

    config.get_settings.cache_clear()
    from partyhub.web.app import app, rebuilder

    monkeypatch.setattr(rebuilder, "schedule", lambda: None)
    with TestClient(app) as c:
        yield c


def _msg(chat_id, mid, text):
    return {"update_id": mid, "message": {"message_id": mid, "date": 1792929600, "chat": {"id": chat_id, "type": "supergroup"},
                                          "from": {"first_name": "Сайт"}, "text": text}}


def test_telegram_webhook_saves_only_leads_chat(client):
    from partyhub.db import session_scope

    client.post("/api/telegram", json=_msg(-100777, 10, "Имя: Анна\nТелефон: 091123456"), headers=TG)
    client.post("/api/telegram", json=_msg(-100999, 11, "Имя: Чужой\nТелефон: 077554433"), headers=TG)
    client.post("/api/telegram", json=_msg(-100777, 10, "Имя: Анна\nТелефон: 091123456"), headers=TG)  # повтор
    with session_scope() as s:
        rows = s.query(Touch).filter_by(system="telegram").all()
        assert [(t.external_id, t.phone, t.source) for t in rows] == [("tg:-100777:10", "+37491123456", "chat")]


# ------------------------------------------------------------ чат Битрикс24
def test_clean_text_keeps_links():
    assert clean_text("[b]Имя:[/b] Анна[br][url=https://a.am/?utm_source=ya]сайт[/url]") == "Имя: Анна\nсайт https://a.am/?utm_source=ya"


def test_bitrix_chat_reads_backwards_then_forward(session, monkeypatch):
    monkeypatch.setenv("BITRIX_CHAT_ID", "55")
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://x.bitrix24.ru/rest/1/abc/")
    from partyhub import config

    config.get_settings.cache_clear()
    msgs = {1: ("2026-10-19T10:00:00+04:00", "Имя: Старая\nТелефон: 091000001"),
            2: ("2026-10-20T10:00:00+04:00", "Имя: Анна\nТелефон: 091123456"),
            3: ("2026-10-20T11:00:00+04:00", "ок, перезвонил")}
    calls = []

    def handler(request):
        import json

        body = json.loads(request.content)
        assert request.url.path.endswith("im.dialog.messages.get.json") and body["DIALOG_ID"] == "chat55"
        calls.append({k: body[k] for k in ("FIRST_ID", "LAST_ID") if k in body})
        ids = sorted(msgs)
        if "FIRST_ID" in body:
            ids = [i for i in ids if i > body["FIRST_ID"]]
        if "LAST_ID" in body:
            ids = [i for i in ids if i < body["LAST_ID"]]
        out = [{"id": i, "date": msgs[i][0], "text": msgs[i][1], "author_id": 7} for i in ids]
        return httpx.Response(200, json={"result": {"messages": out, "users": [{"id": 7, "name": "Менеджер"}]}})

    c = BitrixChatCollector(BitrixClient("https://x.bitrix24.ru/rest/1/abc/", http=httpx.Client(transport=httpx.MockTransport(handler))))
    assert c.collect(session, datetime(2026, 10, 20).date(), datetime(2026, 10, 20).date()) == 1  # старое сообщение до периода
    msgs[4] = ("2026-10-21T09:00:00+04:00", "Карен 077554433")
    assert c.collect(session, datetime(2026, 10, 21).date(), datetime(2026, 10, 21).date()) == 1
    assert calls[-1] == {"FIRST_ID": 3}  # продолжили с последнего прочитанного, включая сообщение без заявки
    phones = sorted(t.phone for t in session.query(Touch).filter_by(system="bitrix_chat"))
    assert phones == ["+37477554433", "+37491123456"]


# ------------------------------------------------------------ Google-таблица
class FakeSheet:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.tab_list = []

    def tabs(self):
        return self.tab_list

    def add_tab(self, title):
        self.tab_list.append(title)

    def get(self, rng):
        return [list(r) for r in self.rows]

    def put(self, rng, rows):
        self.rows[:1] = rows

    def append(self, rng, rows):
        self.rows.extend(rows)

    def batch_put(self, data):
        for d in data:
            cell = d["range"].split("!")[1]
            col, row = "ABCDEFG".index(cell[0]), int(cell[1:])
            r = self.rows[row - 1]
            r += [""] * (7 - len(r))
            r[col] = d["values"][0][0]


def _touch(session, ext, at, phone, name=None, system="telegram", source="chat", **kw):
    session.add(Touch(source=source, system=system, external_id=ext, created_at=at, phone=phone, name=name,
                      extra={"comment": f"текст {ext}"}, **kw))
    session.flush()


def test_sheet_sync_adds_once_and_keeps_human_edits(fixed_now, session, monkeypatch):
    from partyhub import sheets
    from partyhub.pipeline.merge import rebuild_leads

    _touch(session, "tg:1:1", datetime(2026, 10, 20, 14, 5), "+37491123456")
    _touch(session, "calls:9", datetime(2026, 10, 20, 16, 0), "+37491123456", system="callibri", source="call",
           utm_source="yandex", utm_medium="cpc", utm_campaign="712345", utm_content="context|games.am")
    _touch(session, "calls:10", datetime(2026, 10, 20, 17, 0), "+37477554433", name="=HYPERLINK(1)", system="callibri", source="call")
    rebuild_leads(session)
    sheet = FakeSheet()
    since = datetime(2026, 10, 1)
    assert sheets.sync(session, sheet, since) == {"added": 2, "updated_cells": 0}
    assert sheet.tab_list == ["Заявки"] and sheet.rows[0] == sheets.HEADER
    first = sheet.rows[1]
    assert first[:2] == ["2026-10-20 14:05", "'+374 91 123456"]
    assert first[3] == "Telegram · Директ · РСЯ" and first[4] == "yandex / cpc / 712345 / context|games.am"
    assert first[5] == "текст tg:1:1" and first[6] == "telegram:tg:1:1"
    assert sheet.rows[2][2] == "'=HYPERLINK(1)"  # формула из чата не выполнится

    sheet.rows[1][5] = "Перезвонить в пятницу"  # менеджер пишет комментарий
    sheet.rows[1][2] = ""
    _touch(session, "tg:1:2", datetime(2026, 10, 20, 18, 0), "+37491123456", name="Анна")
    rebuild_leads(session)
    assert sheets.sync(session, sheet, since) == {"added": 0, "updated_cells": 1}
    assert sheet.rows[1][2] == "Анна" and sheet.rows[1][5] == "Перезвонить в пятницу" and len(sheet.rows) == 3


def test_sheet_sync_finds_row_by_phone_when_first_touch_changes(fixed_now, session):
    from partyhub import sheets
    from partyhub.pipeline.merge import rebuild_leads

    _touch(session, "tg:1:1", datetime(2026, 10, 20, 14, 5), "+37491123456")
    rebuild_leads(session)
    sheet = FakeSheet()
    sheets.sync(session, sheet, datetime(2026, 10, 1))
    # Callibri догрузил более ранний звонок того же клиента: ключ заявки сменился, но строка та же.
    sheet.rows[1][0] = (datetime(2026, 10, 20, 14, 5) - datetime(1899, 12, 30)).total_seconds() / 86400
    _touch(session, "calls:1", datetime(2026, 10, 20, 9, 0), "+37491123456", system="callibri", source="call")
    rebuild_leads(session)
    assert sheets.sync(session, sheet, datetime(2026, 10, 1))["added"] == 0 and len(sheet.rows) == 2


def test_sheet_refuses_foreign_header(session):
    from partyhub import sheets

    sheet = FakeSheet([["Что-то своё"]])
    sheet.tab_list = ["Заявки"]
    with pytest.raises(RuntimeError, match="шапкой"):
        sheets.sync(session, sheet, datetime(2026, 10, 1))


def test_frequent_leads_sync_does_not_count_as_morning_run(session, fixed_now, monkeypatch):
    import partyhub.jobs as jobs

    monkeypatch.setattr(jobs, "today_local", lambda: datetime(2026, 10, 21).date())
    res = jobs.leads_sync()
    assert [c["collector"] for c in res["collect"]] == list(jobs.FAST_SOURCES) and res["sheet"] is None
    assert jobs._ran_today("daily") is False  # иначе утренний цикл, пропущенный ночью, не догнался бы
