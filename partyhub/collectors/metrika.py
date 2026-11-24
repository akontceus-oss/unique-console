"""Яндекс Метрика: визиты по UTM-меткам и загрузка офлайн-конверсий.

API отчётов: https://yandex.ru/dev/metrika/ru/stat/
Офлайн-конверсии: https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversion/upload
Статус загрузки: https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversions/findById_1
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings, load_rules
from ..models import ConversionUpload, Lead, MetrikaSourceStat, SyncCursor, Touch, TrafficStat, WebStat
from ..pipeline.normalize import now_local, parse_utm_content
from .base import Collector

STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"
MGMT_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/offline_conversions/upload"
UPLOADING_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/offline_conversions/uploading/{id}"
# Итоговые статусы загрузки: дальше Метрика их не меняет.
FINAL_STATUSES = ("PROCESSED", "LINKAGE_FAILURE")

# Для сверки источников: как Метрика сама разложила визиты и цель «заявка».
SOURCE_DIMENSIONS = ["ym:s:date", "ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine", "ym:s:UTMSource", "ym:s:UTMMedium"]

DIMENSIONS = [
    "ym:s:date",
    "ym:s:UTMSource",
    "ym:s:UTMMedium",
    "ym:s:UTMCampaign",
    "ym:s:UTMContent",
    "ym:s:UTMTerm",
]

# Источники трафика для вкладки «Дашборд»: заходы из Яндекс/Google Поиска, Instagram и т. д.
# «Последний значимый переход» — как в отчёте Метрики «Источники, сводка».
TRAFFIC_DIMENSIONS = [
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSearchEngineRoot",
    "ym:s:lastsignSocialNetwork",
]
# При первом сборе источников трафика берём полгода истории, чтобы на дашборде сразу было с чем сравнивать.
TRAFFIC_BACKFILL_DAYS = 180


API_URL = "https://api-metrika.yandex.net/management/v1"


class MetrikaClient:
    """counter_id — один счётчик или несколько через запятую: отчёты складываются по всем,
    офлайн-конверсии уходят в первый."""

    def __init__(self, token: str, counter_id: str, http: httpx.Client | None = None):
        if not token or not counter_id:
            raise ValueError("Не заданы METRIKA_TOKEN и METRIKA_COUNTER_ID")
        self.counter_ids = [c.strip() for c in str(counter_id).split(",") if c.strip()]
        self.counter_id = ",".join(self.counter_ids)
        self.headers = {"Authorization": f"OAuth {token}"}
        self.http = http or httpx.Client(timeout=120)

    @classmethod
    def for_token(cls, token: str, http: httpx.Client | None = None) -> "MetrikaClient":
        """Клиент без счётчика — для списка счётчиков и целей на экране «Подключения»."""
        c = cls(token, "0", http)
        c.counter_ids, c.counter_id = [], ""
        return c

    def counters(self) -> list[dict]:
        r = self.http.get(f"{API_URL}/counters", headers=self.headers, params={"per_page": 1000})
        r.raise_for_status()
        return [{"id": str(c["id"]), "name": c.get("name") or "", "site": c.get("site") or (c.get("site2") or {}).get("site", "")}
                for c in r.json().get("counters", [])]

    def goals(self, counter_id: str) -> list[dict]:
        r = self.http.get(f"{API_URL}/counter/{int(counter_id)}/goals", headers=self.headers)
        r.raise_for_status()
        out = []
        for g in r.json().get("goals", []):
            # у цели «JavaScript-событие» идентификатор лежит в условии: {"type": "exact", "url": "booking"}
            ident = next((c.get("url") for c in g.get("conditions") or [] if g.get("type") == "action" and c.get("url")), "")
            out.append({"id": str(g["id"]), "name": g.get("name") or "", "type": g.get("type") or "", "identifier": ident or ""})
        return out

    def stat(self, date_from: date, date_to: date, dimensions: list[str], metrics: list[str], filters: str = "") -> list[dict]:
        out, offset, limit = [], 1, 10000
        while True:
            params = {
                "ids": self.counter_id,
                "date1": date_from.isoformat(),
                "date2": date_to.isoformat(),
                "dimensions": ",".join(dimensions),
                "metrics": ",".join(metrics),
                "accuracy": "full",
                "limit": limit,
                "offset": offset,
            }
            if filters:
                params["filters"] = filters
            r = self.http.get(STAT_URL, headers=self.headers, params=params)
            r.raise_for_status()
            data = r.json()
            for row in data.get("data", []):
                dims = [d.get("name") if isinstance(d, dict) else d for d in row["dimensions"]]
                # id — машинное имя («organic», «yandex», «instagram»); у даты и UTM его нет
                keys = [(d.get("id") or d.get("name")) if isinstance(d, dict) else d for d in row["dimensions"]]
                out.append({"dims": dims, "keys": keys, "metrics": row["metrics"]})
            total = data.get("total_rows", 0)
            offset += limit
            if offset > total:
                return out

    def upload_offline_conversions(self, rows: list[dict], id_type: str = "CLIENT_ID") -> dict:
        """rows: [{client_id, target, datetime (unix), price, currency}]"""
        buf = io.StringIO()
        id_col = "ClientId" if id_type == "CLIENT_ID" else "Yclid"
        w = csv.writer(buf)
        w.writerow([id_col, "Target", "DateTime", "Price", "Currency"])
        for r in rows:
            w.writerow([r["client_id"], r["target"], r["datetime"], r.get("price") or "", r.get("currency") or ""])
        r = self.http.post(
            MGMT_URL.format(counter=self.counter_ids[0]),
            headers=self.headers,
            params={"client_id_type": id_type},
            files={"file": ("conversions.csv", buf.getvalue().encode("utf-8"), "text/csv")},
        )
        r.raise_for_status()
        return r.json()

    def uploading(self, upload_id: str) -> dict:
        """Состояние загрузки офлайн-конверсий: {id, status, line_quantity, …}."""
        r = self.http.get(UPLOADING_URL.format(counter=self.counter_ids[0], id=int(upload_id)), headers=self.headers)
        r.raise_for_status()
        return r.json().get("uploading") or {}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class MetrikaCollector(Collector):
    name = "metrika"

    def __init__(self, client: MetrikaClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(self._client or (s.metrika_token and s.metrika_counter_id))

    @property
    def client(self) -> MetrikaClient:
        if self._client is None:
            s = get_settings()
            self._client = MetrikaClient(s.metrika_token, s.metrika_counter_id)
        return self._client

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        s = get_settings()
        goals = s.site_goal_ids
        metrics = ["ym:s:visits", "ym:s:bounceRate", "ym:s:avgVisitDurationSeconds"]
        metrics += [f"ym:s:goal{g}reaches" for g in goals]
        rows = self.client.stat(date_from, date_to, DIMENSIONS, metrics)
        session.execute(delete(WebStat).where(WebStat.date.between(date_from, date_to)))
        for r in rows:
            d, src, med, camp, content, term = [(x or "") for x in r["dims"]]
            m = r["metrics"]
            visits = int(_num(m[0]))
            reached = {g: int(_num(v)) for g, v in zip(goals, m[3:])}
            _, placement = parse_utm_content(content)
            session.add(
                WebStat(
                    date=date.fromisoformat(d),
                    utm_source=src,
                    utm_medium=med,
                    utm_campaign=camp,
                    utm_content=content,
                    utm_term=term,
                    placement=(placement or ""),
                    visits=visits,
                    bounces=int(round(visits * _num(m[1]) / 100)),
                    duration_sum=int(round(visits * _num(m[2]))),
                    goal_reaches=reached.get(s.metrika_goal_id.strip(), 0),
                    goals={g: n for g, n in reached.items() if n} or None,
                )
            )
        self._collect_sources(session, date_from, date_to)
        return len(rows) + self.collect_traffic(session, date_from, date_to)

    def collect_traffic(self, session: Session, date_from: date, date_to: date) -> int:
        """Визиты по источникам трафика (поисковики, соцсети, реклама, прямые) — для вкладки «Дашборд»."""
        goal = get_settings().metrika_goal_id.strip()
        metrics = ["ym:s:visits", "ym:s:bounceRate"] + ([f"ym:s:goal{goal}reaches"] if goal else [])
        cursor_key = f"metrika:traffic_backfill:{self.client.counter_id}"[:100]
        cursor = session.get(SyncCursor, cursor_key)
        if cursor is None:
            date_from = min(date_from, date_to - timedelta(days=TRAFFIC_BACKFILL_DAYS - 1))
        rows = self.client.stat(date_from, date_to, TRAFFIC_DIMENSIONS, metrics)
        session.execute(delete(TrafficStat).where(TrafficStat.date.between(date_from, date_to)))
        for r in rows:
            keys = r.get("keys") or r["dims"]
            d, src, engine, network = [str(x or "") for x in keys]
            m = r["metrics"]
            visits = int(_num(m[0]))
            session.add(TrafficStat(date=date.fromisoformat(d[:10]), source=src[:64], engine=engine[:128], network=network[:128],
                                    visits=visits, bounces=int(round(visits * _num(m[1]) / 100)),
                                    goal_reaches=int(_num(m[2])) if len(m) > 2 else 0))
        if cursor is None:
            session.add(SyncCursor(key=cursor_key, value=date_from.isoformat()))
        return len(rows)

    def _collect_sources(self, session: Session, date_from: date, date_to: date) -> None:
        """Цель «заявка» по источникам трафика Метрики. Сбой не мешает основному сбору."""
        goal = get_settings().metrika_goal_id.strip()
        if not goal:
            return
        try:
            rows = self.client.stat(date_from, date_to, SOURCE_DIMENSIONS, ["ym:s:visits", f"ym:s:goal{goal}reaches"])
        except Exception as e:
            self.warning = f"сверка источников с Метрикой не собрана: {type(e).__name__}: {e}"[:500]
            return
        session.execute(delete(MetrikaSourceStat).where(MetrikaSourceStat.date.between(date_from, date_to)))
        for r in rows:
            if len(r["dims"]) != len(SOURCE_DIMENSIONS) or len(r["metrics"]) < 2:
                continue
            d, traffic, engine, src, med = [(x or "") for x in r["dims"]]
            try:
                day = date.fromisoformat(d)
            except ValueError:
                continue
            session.add(MetrikaSourceStat(date=day, traffic_source=traffic[:100], source_engine=engine[:255],
                                          utm_source=src[:255], utm_medium=med[:255],
                                          visits=int(_num(r["metrics"][0])), goal_reaches=int(_num(r["metrics"][1]))))


# ------------------------------------------------- офлайн-конверсии ---
def _booked_leads(session: Session, since) -> list[tuple[Lead, list[Touch]]]:
    cfg = load_rules().get("offline_conversions", {})
    leads = session.scalars(
        select(Lead).where(Lead.status.in_(cfg.get("statuses", ["booked"])), Lead.last_touch_at >= since)
    ).all()
    out = []
    for lead in leads:
        touches = session.scalars(select(Touch).where(Touch.lead_id == lead.id).order_by(Touch.created_at)).all()
        out.append((lead, touches))
    return out


def _lead_key(touches: list[Touch]) -> str:
    # Ключ — первое обращение: он не меняется, когда лид в CRM превращается в сделку.
    return f"{touches[0].system}:{touches[0].external_id}"


def _ids(touches: list[Touch]) -> tuple[str | None, str | None, Touch | None]:
    """ClientID и yclid — из самого свежего обращения, где они есть (ближе всего к последнему визиту)."""
    marked = [t for t in touches if t.ym_client_id or t.yclid]
    if not marked:
        return None, None, None
    last = marked[-1]
    cid = next((t.ym_client_id for t in reversed(marked) if t.ym_client_id), None)
    yclid = next((t.yclid for t in reversed(marked) if t.yclid), None)
    return cid, yclid, last


def pending_conversions(session: Session) -> list[dict]:
    """Брони, которые ещё не отправлены в Метрику и у которых есть ClientID или yclid.

    Срок считается от последнего обращения с ClientID/yclid, а не от первого: Метрика привязывает
    конверсию, если с последнего визита посетителя прошло не больше 21 дня. Визиты без заявки консоль
    не видит, поэтому окно `max_age_days` шире — лишнюю строку Метрика просто не привяжет.
    """
    cfg = load_rules().get("offline_conversions", {})
    if not cfg.get("enabled"):
        return []
    target = cfg.get("target", "booking")
    since = now_local() - timedelta(days=cfg.get("max_age_days", 45))
    sent = {k for (k,) in session.execute(select(ConversionUpload.lead_key).where(ConversionUpload.target == target))}
    out = []
    for lead, touches in _booked_leads(session, since):
        if not touches:
            continue
        key = _lead_key(touches)
        if key in sent:
            continue
        cid, yclid, last = _ids(touches)
        if not (cid or yclid) or last.created_at < since:
            continue
        out.append(
            {
                "lead_key": key,
                "client_id": cid or yclid,
                "id_type": "CLIENT_ID" if cid else "YCLID",
                "target": target,
                "at": lead.last_touch_at,
                "amount": float(lead.amount or 0),
            }
        )
    return out


def upload_conversions(session: Session, client: MetrikaClient | None = None) -> dict:
    s = get_settings()
    items = pending_conversions(session)
    if not items:
        return {"uploaded": 0}
    client = client or MetrikaClient(s.metrika_token, s.metrika_counter_id)
    result = {"uploaded": 0, "uploads": []}
    for id_type in ("CLIENT_ID", "YCLID"):
        batch = [i for i in items if i["id_type"] == id_type]
        if not batch:
            continue
        resp = client.upload_offline_conversions(
            [
                {
                    "client_id": i["client_id"],
                    "target": i["target"],
                    "datetime": int(i["at"].replace(tzinfo=ZoneInfo(s.timezone)).timestamp()),
                    "price": i["amount"] or "",
                    "currency": s.currency if i["amount"] else "",
                }
                for i in batch
            ],
            id_type=id_type,
        )
        uploading = resp.get("uploading") or {}
        upload_id = str(uploading.get("id", ""))
        for i in batch:
            session.add(
                ConversionUpload(
                    lead_key=i["lead_key"],
                    target=i["target"],
                    client_id=i["client_id"],
                    id_type=id_type,
                    conversion_at=i["at"],
                    amount=i["amount"],
                    uploaded_at=now_local(),
                    upload_id=upload_id,
                    status=uploading.get("status") or None,
                    status_checked_at=now_local(),
                )
            )
        result["uploaded"] += len(batch)
        result["uploads"].append(upload_id)
    session.flush()
    return result


def refresh_upload_statuses(session: Session, client: MetrikaClient | None = None, days: int = 14) -> dict:
    """Спрашивает у Метрики, чем закончились загрузки за `days` дней, которые ещё не в итоговом статусе."""
    since = now_local() - timedelta(days=days)
    ids = [u for (u,) in session.execute(
        select(ConversionUpload.upload_id).where(
            ConversionUpload.uploaded_at >= since,
            ConversionUpload.upload_id.is_not(None),
            ConversionUpload.upload_id != "",
            (ConversionUpload.status.is_(None)) | (ConversionUpload.status.not_in(FINAL_STATUSES)),
        ).distinct()
    )]
    if not ids:
        return {"checked": 0}
    s = get_settings()
    client = client or MetrikaClient(s.metrika_token, s.metrika_counter_id)
    out: dict[str, str] = {}
    for upload_id in ids:
        status = str(client.uploading(upload_id).get("status") or "")
        if not status:
            continue
        out[upload_id] = status
        for row in session.scalars(select(ConversionUpload).where(ConversionUpload.upload_id == upload_id)):
            row.status, row.status_checked_at = status, now_local()
    session.flush()
    return {"checked": len(ids), "statuses": out}


GOAL_CURSOR = "metrika_offline_goal"


def check_offline_goal(session: Session, client: MetrikaClient | None = None) -> dict:
    """Есть ли в счётчике цель с идентификатором из offline_conversions.target. Результат — в SyncCursor,
    чтобы дашборд показывал его без запроса к Метрике."""
    import json

    s = get_settings()
    target = load_rules().get("offline_conversions", {}).get("target", "booking")
    client = client or MetrikaClient(s.metrika_token, s.metrika_counter_id)
    counter = client.counter_ids[0]
    goal = next((g for g in client.goals(counter) if g.get("identifier") == target), None)
    res = {"counter": counter, "target": target, "found": bool(goal), "goal_id": (goal or {}).get("id", ""),
           "goal_name": (goal or {}).get("name", ""), "checked_at": now_local().isoformat(timespec="minutes")}
    row = session.get(SyncCursor, GOAL_CURSOR)
    value = json.dumps(res, ensure_ascii=False)[:255]
    if row:
        row.value = value
    else:
        session.add(SyncCursor(key=GOAL_CURSOR, value=value))
    session.flush()
    return res


def offline_summary(session: Session, d1: date, d2: date) -> dict:
    """Что с бронями за период: сколько ушло в Метрику, что она ответила, и почему остальные не ушли.

    Только из базы, без запросов к Метрике: статусы и проверку цели обновляет утренний цикл.
    """
    import json
    from datetime import datetime as _dt

    from ..pipeline.metrics import DIRECT_LABELS

    cfg = load_rules().get("offline_conversions", {})
    st = get_settings()
    target = cfg.get("target", "booking")
    max_age = int(cfg.get("max_age_days", 45))
    start, end = _dt.combine(d1, _dt.min.time()), _dt.combine(d2 + timedelta(days=1), _dt.min.time())
    uploads = {u.lead_key: u for u in session.scalars(select(ConversionUpload).where(ConversionUpload.target == target))}
    counts = {"bookings": 0, "direct_bookings": 0, "sent": 0, "processed": 0, "in_progress": 0, "failed": 0,
              "waiting": 0, "no_id": 0, "no_id_direct": 0, "too_old": 0}
    no_id_by_source: dict[str, int] = {}
    edge = now_local() - timedelta(days=max_age)
    for lead, touches in _booked_leads(session, start):
        if lead.last_touch_at >= end:
            continue
        direct = lead.source_label in DIRECT_LABELS
        counts["bookings"] += 1
        counts["direct_bookings"] += int(direct)
        up = uploads.get(_lead_key(touches)) if touches else None
        if up:
            counts["sent"] += 1
            if up.status == "PROCESSED":
                counts["processed"] += 1
            elif up.status == "LINKAGE_FAILURE":
                counts["failed"] += 1
            else:
                counts["in_progress"] += 1
            continue
        cid, yclid, last = _ids(touches) if touches else (None, None, None)
        if not (cid or yclid):
            counts["no_id"] += 1
            counts["no_id_direct"] += int(direct)
            label = lead.source_label or "Без источника"
            no_id_by_source[label] = no_id_by_source.get(label, 0) + 1
        elif last.created_at < edge:
            counts["too_old"] += 1
        else:
            counts["waiting"] += 1
    rows = session.execute(
        select(ConversionUpload.upload_id, ConversionUpload.uploaded_at, ConversionUpload.id_type, ConversionUpload.status)
        .where(ConversionUpload.target == target).order_by(ConversionUpload.uploaded_at.desc()).limit(500)
    ).all()
    recent: dict[str, dict] = {}
    for upload_id, at, id_type, status in rows:
        r = recent.setdefault(upload_id or at.isoformat(), {"upload_id": upload_id or "", "uploaded_at": at.isoformat(timespec="minutes"),
                                                          "id_type": id_type, "status": status or "", "rows": 0})
        r["rows"] += 1
    goal_row = session.get(SyncCursor, GOAL_CURSOR)
    try:
        goal = json.loads(goal_row.value) if goal_row else {}
    except ValueError:
        goal = {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured": bool(st.metrika_token and st.metrika_counter_id),
        "target": target,
        "max_age_days": max_age,
        "goal_checked": bool(goal) and goal.get("target") == target,
        "goal_found": bool(goal.get("found")) and goal.get("target") == target,
        "goal_counter": goal.get("counter", ""),
        "goal_name": goal.get("goal_name", ""),
        "goal_checked_at": goal.get("checked_at", ""),
        **counts,
        "no_id_by_source": ", ".join(f"{k} — {v}" for k, v in sorted(no_id_by_source.items(), key=lambda kv: -kv[1])),
        "uploads": list(recent.values())[:8],
    }
