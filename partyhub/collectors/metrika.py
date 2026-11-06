"""Яндекс Метрика: визиты по UTM-меткам и загрузка офлайн-конверсий.

API отчётов: https://yandex.ru/dev/metrika/ru/stat/
Офлайн-конверсии: https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversion/upload
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
from ..models import ConversionUpload, Lead, MetrikaSourceStat, Touch, WebStat
from ..pipeline.normalize import now_local, parse_utm_content
from .base import Collector

STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"
MGMT_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/offline_conversions/upload"

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
        return [{"id": str(g["id"]), "name": g.get("name") or "", "type": g.get("type") or ""} for g in r.json().get("goals", [])]

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
                out.append({"dims": dims, "metrics": row["metrics"]})
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
def pending_conversions(session: Session) -> list[dict]:
    """Брони, которые ещё не отправлены в Метрику и у которых есть ClientID или yclid."""
    cfg = load_rules().get("offline_conversions", {})
    if not cfg.get("enabled"):
        return []
    target = cfg.get("target", "booking")
    since = now_local() - timedelta(days=cfg.get("max_age_days", 21))
    sent = {k for (k,) in session.execute(select(ConversionUpload.lead_key).where(ConversionUpload.target == target))}
    out = []
    leads = session.scalars(
        select(Lead).where(Lead.status.in_(cfg.get("statuses", ["booked"])), Lead.first_touch_at >= since)
    ).all()
    for lead in leads:
        touches = session.scalars(select(Touch).where(Touch.lead_id == lead.id).order_by(Touch.created_at)).all()
        if not touches:
            continue
        # Ключ — первое обращение: он не меняется, когда лид в CRM превращается в сделку.
        key = f"{touches[0].system}:{touches[0].external_id}"
        if key in sent:
            continue
        cid = next((t.ym_client_id for t in touches if t.ym_client_id), None)
        yclid = next((t.yclid for t in touches if t.yclid), None)
        if not (cid or yclid):
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
        upload_id = str((resp.get("uploading") or {}).get("id", ""))
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
                )
            )
        result["uploaded"] += len(batch)
        result["uploads"].append(upload_id)
    session.flush()
    return result
