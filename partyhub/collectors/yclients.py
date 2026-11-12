"""YCLIENTS: брони (записи клиентов) — для сверки, какие брони пришли из Директа, а какие из других источников.

API: https://developers.yclients.com/ — GET https://api.yclients.com/api/v1/records/{company_id}
Заголовки: Authorization: Bearer <токен партнёра>, User <токен пользователя>; Accept: application/vnd.yclients.v2+json.
Токен партнёра выдаётся в кабинете разработчика YCLIENTS, токен пользователя — POST /auth (логин и пароль
сотрудника), его вписывают в «Подключения» один раз.

Брони кладутся в crm_records (entity = booking): склейка заявок привязывает их к обращениям по телефону,
как сделки Битрикс24. Бронь без обращения — отдельная заявка без источника («пришли сами»).
"""
from __future__ import annotations

from datetime import date, datetime

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CrmRecord
from ..pipeline.normalize import normalize_phone, parse_dt
from .base import Collector

BASE = "https://api.yclients.com/api/v1"
PAGE = 200  # максимум записей на страницу в YCLIENTS


class YclientsClient:
    def __init__(self, partner_token: str, user_token: str, http: httpx.Client | None = None):
        if not partner_token or not user_token:
            raise ValueError("Не заданы токен партнёра и токен пользователя YCLIENTS")
        self.headers = {"Authorization": f"Bearer {partner_token}, User {user_token}",
                        "Accept": "application/vnd.yclients.v2+json", "Content-Type": "application/json"}
        self.http = http or httpx.Client(timeout=60)

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.http.get(f"{BASE}/{path}", headers=self.headers, params=params or {})
        data = r.json() if r.content else {}
        if r.status_code >= 400 or data.get("success") is False:
            msg = (data.get("meta") or {}).get("message") or r.text[:200]
            raise RuntimeError(f"YCLIENTS {r.status_code}: {msg}")
        return data

    def company(self, company_id: str) -> dict:
        return self.get(f"company/{int(company_id)}").get("data") or {}

    def records(self, company_id: str, date_from: date, date_to: date) -> list[dict]:
        """Записи, созданные в период (c_start_date/c_end_date): бронь считается в день, когда её сделали."""
        out, page = [], 1
        while True:
            data = self.get(f"records/{int(company_id)}", {"c_start_date": date_from.isoformat(), "c_end_date": date_to.isoformat(),
                                                           "page": page, "count": PAGE, "include_finance_transactions": 0})
            batch = data.get("data") or []
            out += batch
            if len(batch) < PAGE:
                return out
            page += 1


def booking_status(rec: dict) -> str:
    """Статус брони в терминах платформы: done — пришёл, booked — ждём, prepaid — с предоплатой, lost — отменил/не пришёл."""
    if rec.get("deleted"):
        return "lost"
    attendance = rec.get("visit_attendance", rec.get("attendance", 0))
    if attendance == 1:
        return "done"
    if attendance == -1:
        return "lost"
    if float(rec.get("prepaid_confirmed") or 0) or rec.get("prepaid") in (True, 1):
        return "prepaid"
    return "booked"


def booking_amount(rec: dict) -> float:
    services = rec.get("services") or []
    total = sum(float(s.get("cost") or s.get("cost_to_pay") or 0) for s in services)
    return total or float(rec.get("cost") or 0)


def parse_records(records: list[dict]) -> list[dict]:
    rows = []
    for rec in records:
        client = rec.get("client") or {}
        created = parse_dt(str(rec.get("create_date") or rec.get("date") or "")) or datetime.now()
        visit = parse_dt(str(rec.get("datetime") or rec.get("date") or ""))
        rows.append({
            "entity": "booking",
            "external_id": str(rec.get("id")),
            "phone": normalize_phone(str(client.get("phone") or "")) or None,
            "title": (client.get("name") or client.get("display_name") or None),
            "status_raw": "deleted" if rec.get("deleted") else str(rec.get("visit_attendance", rec.get("attendance", 0))),
            "status": booking_status(rec),
            "amount": booking_amount(rec),
            "created_at": created.replace(tzinfo=None),
            "modified_at": None,
            # «Сотрудник» в YCLIENTS — комната (ресурс брони), а не менеджер: при склейке заявок его не берём
            "manager": ((rec.get("staff") or {}).get("name") or None),
            "party_date": visit.date() if visit else None,
            "guests": None,
            "lost_reason": "отменена" if rec.get("deleted") else ("не пришёл" if rec.get("visit_attendance") == -1 else None),
        })
    return rows


class YclientsCollector(Collector):
    name = "yclients"

    def __init__(self, client: YclientsClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(self._client or (s.yclients_partner_token and s.yclients_user_token and s.yclients_company_id))

    @property
    def client(self) -> YclientsClient:
        if self._client is None:
            s = get_settings()
            self._client = YclientsClient(s.yclients_partner_token, s.yclients_user_token)
        return self._client

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        rows = parse_records(self.client.records(get_settings().yclients_company_id, date_from, date_to))
        ids = [r["external_id"] for r in rows]
        if ids:
            session.execute(delete(CrmRecord).where(CrmRecord.entity == "booking", CrmRecord.external_id.in_(ids)))
        for r in rows:
            session.add(CrmRecord(**r))
        return len(rows)


# ------------------------------------------------------------ выгрузка файлом ---
# Пока нет токена партнёра, брони загружают вручную: «Записи» в YCLIENTS → «Выгрузить в Excel» →
# кнопка «Загрузить выгрузку» на карточке YCLIENTS. Колонки ищем по названию: порядок и лишние колонки не важны.
FILE_COLUMNS = {
    "id": ("id записи", "номер записи", "№ записи", "id", "№"),
    "created": ("дата создания", "дата и время создания", "создал дата", "создана", "создано", "создан"),
    "visit": ("дата и время визита", "дата визита", "время визита", "дата и время записи", "дата записи", "дата сеанса", "дата и время", "дата", "начало"),
    "time": ("время визита", "время записи", "время начала", "время"),
    "client": ("имя клиента", "клиент имя", "клиент", "фио", "имя"),
    "phone": ("телефон клиента", "телефон", "номер телефона"),
    "staff": ("сотрудник имя", "сотрудник", "мастер", "специалист", "ресурс"),
    "services": ("услуги название", "услуги", "услуга", "название услуги"),
    "amount": ("стоимость с учетом скидки", "стоимость", "сумма", "итого", "к оплате", "цена"),
    "status": ("статус визита", "статус записи", "статус", "посещение", "визит"),
    "label": ("категория записи", "категория"),  # метки записи: «ПРЕДОПЛАТА ВНЕСЕНА»
}
FILE_LIMIT = 15 * 1024 * 1024


def _norm(v) -> str:
    return " ".join(str(v or "").replace("ё", "е").replace("Ё", "Е").lower().replace("\xa0", " ").split())


def find_columns(header: list) -> dict[str, int]:
    """Название поля → номер колонки. Сначала точные совпадения по всем полям, потом вхождения."""
    names = [_norm(h) for h in header]
    out: dict[str, int] = {}
    for exact in (True, False):
        for field, aliases in FILE_COLUMNS.items():
            if field in out:
                continue
            for a in aliases:
                a = _norm(a)
                hit = next((i for i, n in enumerate(names) if n and i not in out.values()
                            and (n == a if exact else (len(a) > 2 and a in n))), None)
                if hit is not None:
                    out[field] = hit
                    break
    if "time" in out and out.get("created") is not None and "созд" in names[out["time"]]:
        out.pop("time")
    return out


def _read_table(data: bytes, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        from io import BytesIO

        from openpyxl import load_workbook

        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    if name.endswith(".xls") or data[:4] == b"\xd0\xcf\x11\xe0":
        return _read_xls(data)
    import csv
    from io import StringIO

    text = next((data.decode(enc) for enc in ("utf-8-sig", "cp1251") if _decodes(data, enc)), data.decode("utf-8", "replace"))
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t") if text.strip() else csv.excel
    return [list(r) for r in csv.reader(StringIO(text), dialect)]


def _read_xls(data: bytes) -> list[list]:
    """«Отчёт по записям» YCLIENTS — старый формат .xls. Файл YCLIENTS слегка битый, Excel это прощает, xlrd — по флагу."""
    import xlrd

    book = xlrd.open_workbook(file_contents=data, ignore_workbook_corruption=True)
    sh = book.sheet_by_index(0)
    out = []
    for i in range(sh.nrows):
        row = []
        for c in sh.row(i):
            v = c.value
            if c.ctype == xlrd.XL_CELL_DATE:
                v = xlrd.xldate_as_datetime(v, book.datemode)
            elif c.ctype == xlrd.XL_CELL_NUMBER and float(v).is_integer():
                v = int(v)  # телефон 79257853334.0 → 79257853334
            row.append(v)
        out.append(row)
    return out


def _two_row_header(top: list, sub: list) -> list | None:
    """Шапка в две строки («Клиент» над «Имя | Телефон»): склеиваем, протягивая объединённые ячейки вправо."""
    if not any(isinstance(v, str) and v.strip() for v in sub) or any(isinstance(v, (int, float, datetime)) for v in sub):
        return None
    if any(ch.isdigit() for v in sub for ch in str(v or "")):  # в подзаголовках нет цифр, в строке данных есть
        return None
    out, last = [], ""
    for i in range(max(len(top), len(sub))):
        t = str((top[i] if i < len(top) else "") or "").strip()
        last = t or last
        b = str((sub[i] if i < len(sub) else "") or "").strip()
        out.append(" ".join(x for x in (last if (t or b) else "", b) if x))
    return out


def _decodes(data: bytes, enc: str) -> bool:
    try:
        data.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


def _dt(v, time_value=None) -> datetime | None:
    if isinstance(v, datetime):
        d = v
    elif isinstance(v, date):
        d = datetime(v.year, v.month, v.day)
    else:
        s = str(v or "").strip().replace(" в ", " ")
        d = parse_dt(s) or parse_dt(s[:16]) if s else None
        if d and d.tzinfo:
            d = d.replace(tzinfo=None)
    if d and time_value not in (None, "") and d.hour == 0 and d.minute == 0:
        t = time_value if hasattr(time_value, "hour") else parse_dt(f"01.01.2000 {str(time_value).strip()[:5]}")
        if t:
            d = d.replace(hour=t.hour, minute=t.minute)
    return d


def _money(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v or "").replace(",", ".") if ch.isdigit() or ch in ".-")
    try:
        return float(s) if s not in ("", ".", "-") else 0.0
    except ValueError:
        return 0.0


def file_status(raw: str) -> str:
    """Статус из текста выгрузки: «Клиент пришёл», «Клиент не пришёл», «Ожидание клиента», «Удалена»…"""
    s = _norm(raw)
    if s in ("1", "-1", "0", "2"):
        return booking_status({"visit_attendance": int(s)})
    if any(w in s for w in ("удал", "отмен", "не пришел", "неявк")):
        return "lost"
    if "пришел" in s or "был" in s or "заверш" in s:
        return "done"
    if "предоплат" in s:
        return "prepaid"
    return "booked"


def parse_export(data: bytes, filename: str = "") -> dict:
    """Файл выгрузки → строки crm_records (entity = booking) и что не удалось разобрать."""
    if len(data) > FILE_LIMIT:
        raise ValueError("Файл больше 15 МБ: выгрузите период покороче")
    table = [r for r in _read_table(data, filename) if any(c not in (None, "") for c in r)]
    head_at, cols, header = None, {}, []
    for i, r in enumerate(table[:20]):  # над шапкой бывают строки с названием отчёта и периодом
        two = _two_row_header(r, table[i + 1]) if i + 1 < len(table) else None
        for cand, skip in ((two, 1), (r, 0)):
            c = find_columns(cand) if cand else {}
            if "phone" in c and ("visit" in c or "created" in c):
                head_at, cols, header = i + skip, c, cand
                break
        if head_at is not None:
            break
    if head_at is None:
        raise ValueError("Не нашёл шапку с колонками «Телефон» и «Дата». Это точно выгрузка записей YCLIENTS?")
    rows, skipped = [], 0
    get = lambda r, f: r[cols[f]] if f in cols and cols[f] < len(r) else None  # noqa: E731
    for r in table[head_at + 1:]:
        visit = _dt(get(r, "visit"), get(r, "time"))
        created = _dt(get(r, "created")) or visit
        if not created:
            skipped += 1  # итоговые строки, пустые строки
            continue
        phone = normalize_phone(str(get(r, "phone") or "")) or None
        raw = str(get(r, "status") or "").strip()
        rid = get(r, "id")
        if rid not in (None, ""):
            ext = str(int(rid)) if isinstance(rid, float) and rid.is_integer() else str(rid).strip()
        else:
            import hashlib

            # без ID: время создания + телефон + сотрудник не меняются, даже когда меняют статус или услуги
            key = "|".join(str(x) for x in (visit, phone, get(r, "staff"), _dt(get(r, "created")) or get(r, "services")))
            ext = "f" + hashlib.sha1(key.encode()).hexdigest()[:20]
        status = file_status(raw)
        if status == "booked" and "предоплат" in _norm(get(r, "label")):
            status = "prepaid"
        rows.append({
            "entity": "booking", "external_id": ext[:64], "phone": phone,
            "title": (str(get(r, "client") or "").strip() or None),
            "status_raw": raw[:64], "status": status, "amount": _money(get(r, "amount")),
            "created_at": created, "modified_at": None,
            "manager": (str(get(r, "staff") or "").strip()[:255] or None),
            "party_date": visit.date() if visit else None, "guests": None,
            "lost_reason": (raw[:255] or None) if status == "lost" else None,
        })
    return {"rows": rows, "skipped": skipped, "columns": {f: str(header[i]) for f, i in cols.items()},
            "no_created": "created" not in cols}


def import_export(session: Session, data: bytes, filename: str = "") -> dict:
    """Загружает брони из файла. Повторная загрузка того же периода не создаёт дублей: запись ищется по ID."""
    from ..models import CollectorRun
    from ..pipeline.normalize import now_local

    parsed = parse_export(data, filename)
    rows = parsed["rows"]
    if not rows:
        raise ValueError("В файле не нашлось ни одной записи")
    ids = [r["external_id"] for r in rows]
    for i in range(0, len(ids), 500):
        session.execute(delete(CrmRecord).where(CrmRecord.entity == "booking", CrmRecord.external_id.in_(ids[i:i + 500])))
    seen = set()
    for r in rows:
        if r["external_id"] not in seen:
            seen.add(r["external_id"])
            session.add(CrmRecord(**r))
    d1, d2 = min(r["created_at"] for r in rows).date(), max(r["created_at"] for r in rows).date()
    note = f"файл {filename or 'без имени'}" + (": в файле нет даты создания записи, брони посчитаны по дате визита" if parsed["no_created"] else "")
    now = now_local()
    session.add(CollectorRun(started_at=now, finished_at=now, collector="yclients_file", date_from=d1, date_to=d2,
                             status="ok", rows=len(seen), message=note, duration_sec=0))
    session.flush()
    return {"rows": len(seen), "skipped": parsed["skipped"], "date_from": d1.isoformat(), "date_to": d2.isoformat(),
            "without_phone": sum(1 for r in rows if not r["phone"]), "columns": parsed["columns"], "no_created": parsed["no_created"]}
