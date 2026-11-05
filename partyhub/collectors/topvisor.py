"""Топвизор: позиции сайта по запросам.

API v2: https://topvisor.com/api/v2/ — POST https://api.topvisor.com/v2/json/<оператор>/<сервис>/<метод>
Заголовки: User-Id и Authorization: bearer <ключ>. Ключ — в профиле Топвизора.

Проектов и регионов может быть несколько: TOPVISOR_PROJECT_ID=111,222, TOPVISOR_REGION_INDEX=1,2.
Пустой TOPVISOR_REGION_INDEX — все регионы проекта (Яндекс, Google и т. д.).
Список проектов и индексов регионов: python -m partyhub topvisor-projects.
"""
from __future__ import annotations

import time
from datetime import date

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import SeoKeyword, SeoPosition
from .base import Collector

BASE = "https://api.topvisor.com/v2/json"
PAGE = 1000  # запросов за один вызов history
DEVICES = {1: "планшет", 2: "моб."}  # 0 — компьютер, его не подписываем
# Частотность: у Яндекса берём «"запрос"» (тип 2, без хвостов), у Google есть только тип 3.
VOLUME_TYPES = {0: ("volume_yandex", 2), 1: ("volume_google", 3)}


def split_ids(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").replace(";", ",").split(",") if x.strip()]


class TopvisorClient:
    def __init__(self, user_id: str, api_key: str, http: httpx.Client | None = None):
        if not user_id or not api_key:
            raise ValueError("Не заданы TOPVISOR_USER_ID и TOPVISOR_API_KEY")
        self.headers = {"User-Id": str(user_id), "Authorization": f"bearer {api_key}", "Content-Type": "application/json"}
        self.http = http or httpx.Client(timeout=60)

    def call(self, method: str, body: dict) -> dict | list:
        for attempt in range(3):  # обрыв соединения на рукопожатии бывает плавающим (сеть хостинга) — повторяем
            try:
                r = self.http.post(f"{BASE}/{method}", headers=self.headers, json=body)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"Topvisor: {data['errors']}")
        return data.get("result") if data.get("result") is not None else {}

    def projects(self) -> list[dict]:
        """Проекты с поисковиками и регионами: [{id, name, site, searchers: [{key, name, regions: [{index, name}]}]}]."""
        res = self.call("get/projects_2/projects", {"show_searchers_and_regions": 1, "fields": ["id", "name", "site"]})
        return res if isinstance(res, list) else []

    def history(self, project_id: str, region_indexes: list, date_from: date, date_to: date,
                offset: int = 0, limit: int | None = None, extra_fields: list[str] | None = None) -> dict:
        body = {
            "project_id": int(project_id),
            "regions_indexes": [int(i) for i in region_indexes],
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "show_headers": True,
            "positions_fields": ["position", "relevant_url"],
            "fields": ["name", "group_name", *(extra_fields or [])],
            "type_range": 0,  # все проверки за период (по умолчанию Топвизор отдаёт не больше 31 даты)
            "limit": limit or PAGE,
            "offset": offset,
        }
        res = self.call("get/positions_2/history", body)
        return res if isinstance(res, dict) else {}

    def history_all(self, project_id: str, region_indexes: list, date_from: date, date_to: date,
                    extra_fields: list[str] | None = None) -> dict:
        """Все запросы проекта страницами по PAGE: заголовки — из первой страницы, запросы — из всех."""
        first = self.history(project_id, region_indexes, date_from, date_to, extra_fields=extra_fields)
        keywords = list(first.get("keywords") or [])
        page = keywords
        while len(page) >= PAGE:
            page = list(self.history(project_id, region_indexes, date_from, date_to, offset=len(keywords),
                                     extra_fields=extra_fields).get("keywords") or [])
            keywords += page
        return {**first, "keywords": keywords}


WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")


def schedule_of(client: "TopvisorClient", project_id: str) -> list[dict]:
    """Расписание автопроверки позиций: [{"days": ["MONDAY"], "times": [{"hour": 0, "minute": 0}]}]."""
    res = client.call("get/schedule_2", {"type": "positions_go", "target_id": int(project_id)})
    return list((res or {}).get("schedule") or []) if isinstance(res, dict) else []


def set_schedule(client: "TopvisorClient", project_id: str, days: list[str], hour: int, minute: int) -> list[dict]:
    """Одно правило «эти дни недели в это время»; пустой список дней — выключить автопроверку."""
    days = [d for d in WEEKDAYS if d in set(days)]  # порядок недели, без мусора
    if not days:
        client.call("del/schedule_2", {"type": "positions_go", "target_id": int(project_id)})
        return []
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Время: часы 0–23, минуты 0–59")
    # час и минуту Топвизор ждёт строками: на числа отвечает предупреждением в errors
    rule = {"days": ["ALL"] if len(days) == 7 else days, "times": [{"hour": str(hour), "minute": str(minute)}]}
    client.call("edit/schedule_2", {"type": "positions_go", "target_id": int(project_id), "schedule": [rule]})
    return [rule]


def region_names(projects: list[dict]) -> dict[tuple[str, str], tuple[str, str]]:
    """(id проекта, индекс региона) → (название проекта, «Регион · Поисковик»)."""
    out = {}
    for p in projects or []:
        pname = p.get("name") or p.get("site") or str(p.get("id"))
        for se in p.get("searchers") or []:
            for r in se.get("regions") or []:
                label = " · ".join(x for x in (r.get("name"), se.get("name"), DEVICES.get(_int(r.get("device")))) if x)
                out[(str(p.get("id")), str(r.get("index")))] = (pname, label or str(r.get("index")))
    return out


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def volume_fields(project: dict) -> dict[str, str]:
    """Поля частотности для history: {"volume:213:0:2": "volume_yandex", ...} — по первому региону каждого поисковика."""
    out = {}
    for se in project.get("searchers") or []:
        col, kind = VOLUME_TYPES.get(_int(se.get("key")), (None, None))
        regions = se.get("regions") or []
        if col and regions and col not in out.values():
            out[f"volume:{regions[0].get('key')}:{se.get('key')}:{kind}"] = col
    return out


def parse_volumes(result: dict, fields: dict[str, str], project: str | None) -> list[dict]:
    rows = []
    for kw in result.get("keywords") or []:
        row = {"project": project or "", "keyword": kw.get("name") or "", "group_name": kw.get("group_name") or None,
               "volume_yandex": None, "volume_google": None}
        for field, col in fields.items():
            row[col] = _int(kw.get(field))
        rows.append(row)
    return rows


def parse_history(result: dict, names: dict | str | None = None) -> list[dict]:
    """positionsData: {"YYYY-MM-DD:<project>:<region>": {"position": "5" | "--", "relevant_url": "..."}}

    region — ключ «проект:индекс региона» (уникален, даже если запрос есть в двух проектах),
    region_name и project — для показа. names — из region_names() или из заголовков ответа.
    """
    if isinstance(names, str):  # один регион с заданным названием
        names = {None: ("", names)}
    names = dict(names or {})
    names.update({k: v for k, v in region_names((result.get("headers") or {}).get("projects") or []).items() if k not in names})
    rows = []
    for kw in result.get("keywords") or []:
        for key, val in (kw.get("positionsData") or {}).items():
            day, _, rest = key.partition(":")
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            project, _, index = rest.partition(":")
            pname, rname = names.get((project, index)) or names.get(None) or ("", index)
            pos = (val or {}).get("position")
            try:
                position = int(pos)
            except (TypeError, ValueError):
                position = None  # "--" — сайт не найден в выдаче
            rows.append({"date": d, "keyword": kw.get("name") or "", "region": f"{project}:{index}" if rest else rname,
                         "region_name": rname or index, "project": pname or project or None,
                         "group_name": kw.get("group_name") or None, "position": position,
                         "url": (val or {}).get("relevant_url") or None})
    return rows


class TopvisorCollector(Collector):
    name = "topvisor"

    def __init__(self, client: TopvisorClient | None = None):
        self._client = client

    def configured(self) -> bool:
        s = get_settings()
        return bool(self._client or (s.topvisor_user_id and s.topvisor_api_key and s.topvisor_project_id))

    @property
    def client(self) -> TopvisorClient:
        if self._client is None:
            s = get_settings()
            self._client = TopvisorClient(s.topvisor_user_id, s.topvisor_api_key)
        return self._client

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        s = get_settings()
        projects = {str(p.get("id")): p for p in self.client.projects()}
        names = region_names(list(projects.values()))
        wanted = split_ids(s.topvisor_region_index)
        rows, volumes = [], []
        for pid in split_ids(s.topvisor_project_id):
            if pid not in projects:
                raise RuntimeError(f"Проекта {pid} нет в Топвизоре (список: python -m partyhub topvisor-projects)")
            own = sorted({i for (p, i) in names if p == pid}, key=int)
            # регионы выбираются общим списком: у каждого проекта берём только свои
            indexes = [i for i in wanted if i in own] if wanted and any(i in own for i in wanted) else own
            if not indexes:
                continue
            vf = volume_fields(projects[pid])
            res = self.client.history_all(pid, indexes, date_from, date_to, extra_fields=list(vf))
            rows += parse_history(res, names)
            p = projects[pid]
            volumes += parse_volumes(res, vf, p.get("name") or p.get("site") or pid)
        session.execute(delete(SeoPosition).where(SeoPosition.date.between(date_from, date_to)))
        if volumes:  # частотность — на сегодня, старую заменяем целиком
            session.execute(delete(SeoKeyword))
            seen_kw = set()
            for v in volumes:
                if (v["project"], v["keyword"]) not in seen_kw:
                    seen_kw.add((v["project"], v["keyword"]))
                    session.add(SeoKeyword(**v, updated=date_to))
        seen = set()
        for r in rows:
            k = (r["date"], r["keyword"], r["region"])
            if k not in seen:  # дубль запроса в двух группах проекта
                seen.add(k)
                session.add(SeoPosition(**r))
        return len(seen)
