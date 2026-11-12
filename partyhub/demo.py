"""Демо-данные: 90 дней работы условного детского развлекательного центра в Ереване.

Все данные вымышлены: компании, площадки, телефоны и имена сгенерированы.
Генератор детерминирован (seed), поэтому прототип дашборда и платформа показывают одно и то же.

Последние 14 дней генератор прогоняет настоящий модуль правил платформы (в режиме dry_run)
и имитирует решения владельца: так в журнале появляются реальные правки и их эффект.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .collectors.direct import record_changes
from .models import (
    Action,
    AdStat,
    CampaignSnapshot,
    CollectorRun,
    CrmRecord,
    DirectSliceStat,
    Lead,
    MetrikaSourceStat,
    ReportLog,
    SearchQuery,
    SeoPosition,
    SettingsChange,
    Touch,
    TrafficStat,
    WebStat,
)
from .pipeline.merge import rebuild_leads
from .rules.actions import ActionApplier, evaluate_due
from .rules.engine import run_rules


@dataclass
class Campaign:
    id: int
    name: str
    network: str
    daily_budget: float
    cpc: float
    ctr: float
    conv: float  # клики → заявка
    book: float  # заявка → бронь
    keywords: list[str]


CAMPAIGNS = [
    Campaign(701001, "Поиск · День рождения в картинге", "SEARCH", 17000, 420, 0.075, 0.065, 0.46,
             ["день рождения в картинге", "картинг день рождения ребенка", "где отметить день рождения ребенку", "картинг для детей ереван"]),
    Campaign(701002, "Поиск · Детский праздник", "SEARCH", 12000, 360, 0.055, 0.042, 0.34,
             ["детский праздник ереван", "организация детского праздника", "праздник для ребенка 8 лет", "аниматоры на день рождения"]),
    Campaign(701003, "РСЯ · Родители 25–45", "AD_NETWORK", 16000, 110, 0.009, 0.012, 0.28, []),
    Campaign(701004, "РСЯ · Ретаргетинг", "AD_NETWORK", 6000, 140, 0.012, 0.030, 0.38, []),
    Campaign(701005, "Поиск · Бренд", "SEARCH", 3000, 150, 0.21, 0.13, 0.62, ["драйв кидс картинг", "драйв кидс ереван отзывы"]),
]

# площадка: (вес показов, множитель конверсии, CTR, доля отказов, ср. время визита)
PLACEMENTS = {
    "mama-club.am": (1.4, 1.8, 0.011, 0.24, 140),
    "yerevan-afisha.am": (1.2, 1.6, 0.010, 0.27, 120),
    "kids-events.am": (0.9, 1.9, 0.012, 0.22, 150),
    "pogoda24.am": (1.3, 0.8, 0.008, 0.38, 70),
    "dzen.ru": (1.6, 0.9, 0.007, 0.35, 85),
    "recepty-dnya.ru": (1.0, 0.7, 0.008, 0.41, 60),
    "sport-news.am": (0.8, 0.6, 0.007, 0.44, 55),
    "avto-forum.am": (0.7, 0.4, 0.006, 0.52, 40),
    "kino-online.ru": (0.9, 0.3, 0.009, 0.66, 25),
    "skazki-detyam.ru": (0.6, 1.2, 0.010, 0.33, 95),
    "mail-portal.ru": (1.1, 0.5, 0.006, 0.48, 45),
    "free-games-online.ru": (1.2, 0.05, 0.014, 0.86, 9),
    "anekdoty-prikoly.ru": (0.8, 0.05, 0.012, 0.81, 12),
    "horoscope-today.ru": (0.7, 0.1, 0.010, 0.77, 15),
    "download-mp3.ru": (0.6, 0.0, 0.013, 0.88, 7),
    "tv-program.am": (0.17, 0.0, 0.007, 0.78, 16),
    "sonnik-online.ru": (0.15, 0.0, 0.006, 0.81, 14),
    "test-iq-free.ru": (0.16, 0.0, 0.008, 0.74, 20),
    "com.puzzle.blast.free": (1.5, 0.0, 0.045, 0.92, 5),
    "com.solitaire.classic.cards": (1.2, 0.0, 0.041, 0.90, 6),
    "com.bubble.shooter.pop": (1.0, 0.0, 0.052, 0.93, 4),
    "id1458963210": (0.8, 0.0, 0.038, 0.91, 5),
    "com.vpn.fast.secure": (0.7, 0.0, 0.033, 0.89, 6),
    "com.word.cross.puzzle": (1.1, 0.0, 0.047, 0.92, 5),
    "lucky-wheel-games.ru": (0.9, 0.0, 0.016, 0.87, 8),
    "skachat-filmy.ru": (0.5, 0.0, 0.012, 0.79, 15),
    "com.merge.dragons.fun": (1.0, 0.0, 0.049, 0.93, 4),
    "kulinariya-blog.ru": (0.2, 0.0, 0.007, 0.79, 18),
    "pesni-online.ru": (0.2, 0.0, 0.008, 0.82, 14),
    "com.cleaner.phone.boost": (1.6, 0.0, 0.051, 0.94, 4),
}

# Новые площадки, которые РСЯ подключает со временем: площадка → за сколько дней до конца появилась.
LATE_PLACEMENTS = {"com.word.cross.puzzle": 9, "lucky-wheel-games.ru": 7, "skachat-filmy.ru": 5, "com.merge.dragons.fun": 3,
                   "kulinariya-blog.ru": 29, "pesni-online.ru": 29, "com.cleaner.phone.boost": 0}

SEARCH_QUERIES = {
    701001: ["день рождения в картинге", "картинг день рождения ребенку 9 лет", "где отметить день рождения ребенку ереван",
             "картинг для детей цены", "картинг вакансии", "работа в картинге инструктор"],
    701002: ["детский праздник ереван", "организация детского дня рождения", "праздник для ребенка 8 лет идеи",
             "аниматоры на день рождения", "детский праздник своими руками", "раскраска картинг скачать бесплатно"],
    701005: ["драйв кидс картинг", "драйв кидс ереван отзывы", "драйв кидс цены"],
}

# Стратегии с целевой ценой конверсии — для инструмента «Целевая цена конверсии» (цены в микроединицах, как в API).
DEMO_STRATEGY = {
    701001: {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 5_500_000_000, "GoalId": 1001}},
             "Network": {"BiddingStrategyType": "SERVING_OFF"}},
    701002: {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": {"AverageCpa": 7_000_000_000, "GoalId": 1001}},
             "Network": {"BiddingStrategyType": "SERVING_OFF"}},
    701004: {"Search": {"BiddingStrategyType": "SERVING_OFF"},
             "Network": {"BiddingStrategyType": "PAY_FOR_CONVERSION", "PayForConversion": {"Cpa": 4_000_000_000, "GoalId": 1001}}},
}

# Запросы автотаргетинга: (запрос, категория, множитель конверсии).
AUTOTARGETING = {
    701001: [("день рождения в картинге цены", "EXACT", 1.4), ("картинг москва", "BROADER", 0.0),
             ("бесплатные игры для детей", "BROADER", 0.0), ("батуты ереван", "COMPETITOR", 0.2)],
    701002: [("аренда зала для детского праздника", "ALTERNATIVE", 1.1), ("квест на день рождения ребенку", "ALTERNATIVE", 0.9),
             ("сценарий дня рождения для детей", "ACCESSORY", 0.0), ("детские игры онлайн", "BROADER", 0.0)],
}
# Срезы для корректировок: значение → (доля кликов, множитель конверсии).
DEVICES = {"MOBILE": (0.62, 0.75), "DESKTOP": (0.30, 1.45), "TABLET": (0.08, 0.5)}
DEMOGRAPHY = {"GENDER_FEMALE|AGE_25_34": (0.24, 1.5), "GENDER_FEMALE|AGE_35_44": (0.2, 1.3), "GENDER_MALE|AGE_25_34": (0.14, 1.0),
              "GENDER_MALE|AGE_35_44": (0.13, 1.0), "GENDER_FEMALE|AGE_18_24": (0.1, 0.3), "GENDER_MALE|AGE_18_24": (0.09, 0.1),
              "GENDER_FEMALE|AGE_45_54": (0.06, 0.8), "GENDER_MALE|AGE_55": (0.04, 0.2)}

SEO_KEYWORDS = [
    ("картинг ереван", 14, 6), ("детский картинг", 9, 3), ("день рождения в картинге", 18, 5),
    ("где отметить день рождения ребенка", 27, 12), ("детский праздник ереван", 22, 9), ("картинг для детей", 11, 2),
    ("развлечения для детей ереван", 35, 19), ("квест для детей день рождения", 41, 28), ("аренда картинга", 16, 10),
    ("картинг цены", 12, 7), ("выпускной в картинге", 30, 15), ("корпоратив картинг", 24, 18),
]


def _seo_where(kw: str) -> dict:
    group = "Праздники" if any(x in kw for x in ("рожд", "праздник", "выпускн", "квест")) else "Картинг"
    return {"region": "1:213", "region_name": "Ереван · Яндекс", "project": "drive-kids.example", "group_name": group}


NAMES = ["Ани", "Мариам", "Арам", "Лилит", "Нарек", "Елена", "Тигран", "Анна", "Сона", "Гор", "Арпи", "Давид", "Нелли", "Карен"]
MANAGERS = ["Анна", "Гаяне", "Давид"]
LOST_REASONS = ["Дорого", "Не подошла дата", "Выбрали другое место", "Не дозвонились", "Передумали"]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _season(d: date, start: date) -> float:
    weekday = [1.05, 1.08, 1.06, 1.02, 0.96, 0.88, 0.85][d.weekday()]
    trend = 0.92 + 0.16 * ((d - start).days / 90)
    return weekday * trend


class DemoBuilder:
    def __init__(self, session: Session, end: date, days: int = 90, seed: int = 42):
        self.s = session
        self.end = end  # последний день с данными («вчера»)
        self.start = end - timedelta(days=days - 1)
        self.rng = random.Random(seed)
        self.panel_rng = random.Random(seed + 1)  # заявки панели — отдельный генератор, остальное демо не меняется
        self.panel_seq = 0
        self.phone_seq = 0
        self.ext_seq = 0
        self.excluded: set[str] = {"download-mp3.ru"}  # было исключено ещё до платформы
        self.negatives: dict[int, set[str]] = {701001: {"бесплатно"}, 701002: set(), 701003: set(), 701004: set(), 701005: set()}
        self.budget = {c.id: c.daily_budget for c in CAMPAIGNS}
        self.returning: list[str] = []

    # ---------------------------------------------------------- helpers ---
    def phone(self) -> str:
        if self.returning and self.rng.random() < 0.04:
            return self.rng.choice(self.returning)
        self.phone_seq += 1
        prefix = self.rng.choice(["91", "93", "94", "95", "96", "98", "99", "77", "55"])
        num = f"+374{prefix}{(self.phone_seq * 7919 + 104729) % 1_000_000:06d}"
        if self.rng.random() < 0.3:
            self.returning.append(num)
        return num

    def ext(self) -> str:
        self.ext_seq += 1
        return f"demo-{self.ext_seq}"

    def at(self, d: date, hour_from=9, hour_to=21) -> datetime:
        return datetime.combine(d, time(self.rng.randint(hour_from, hour_to - 1), self.rng.randint(0, 59), self.rng.randint(0, 59)))

    # ---------------------------------------------------------- one day ---
    def day(self, d: date):
        rng = self.rng
        k = _season(d, self.start)
        for c in CAMPAIGNS:
            budget = self.budget[c.id] * k * rng.uniform(0.85, 1.12)
            if c.id == 701004 and d == self.end - timedelta(days=2):
                budget *= 1.9  # всплеск расхода — для демонстрации сигнала
            if c.network == "SEARCH":
                clicks = max(0, int(budget / (c.cpc * rng.uniform(0.9, 1.1))))
                impr = int(clicks / (c.ctr * rng.uniform(0.85, 1.15))) if clicks else rng.randint(20, 60)
                cost = round(clicks * c.cpc * rng.uniform(0.92, 1.08), 2)
                self.s.add(AdStat(date=d, platform="direct", campaign_id=c.id, campaign_name=c.name, ad_group_id=c.id * 10 + 1,
                                  ad_group_name="Основная", network="SEARCH", placement="search", impressions=impr, clicks=clicks, cost=cost))
                self.s.add(WebStat(date=d, utm_source="yandex", utm_medium="cpc", utm_campaign=str(c.id), utm_content="search|none",
                                   placement="", visits=int(clicks * 0.93), bounces=int(clicks * 0.93 * rng.uniform(0.15, 0.24)),
                                   duration_sum=int(clicks * 0.93 * rng.uniform(110, 160)), goal_reaches=0))
                self._search_queries(d, c, clicks)
                self._leads(d, c, None, clicks * c.conv * rng.uniform(0.8, 1.2), c.book)
            else:
                days_left = (self.end - d).days
                active = {p: v for p, v in PLACEMENTS.items()
                          if p not in self.excluded and days_left <= LATE_PLACEMENTS.get(p, 999)}
                total_w = sum(v[0] for v in active.values())
                for p, (w, conv_m, ctr, bounce, dur) in active.items():
                    share = budget * w / total_w * rng.uniform(0.7, 1.3)
                    cpc = c.cpc * (0.55 if p.startswith(("com.", "id")) else 1.0) * rng.uniform(0.85, 1.15)
                    clicks = _poisson(rng, share / cpc)
                    if clicks == 0 and rng.random() < 0.5:
                        continue
                    impr = max(clicks, int(clicks / (ctr * rng.uniform(0.8, 1.2)))) if clicks else rng.randint(50, 300)
                    cost = round(clicks * cpc, 2)
                    self.s.add(AdStat(date=d, platform="direct", campaign_id=c.id, campaign_name=c.name, ad_group_id=c.id * 10 + 1,
                                      ad_group_name="Интересы" if c.id == 701003 else "Посетители сайта", network="AD_NETWORK",
                                      placement=p, impressions=impr, clicks=clicks, cost=cost))
                    visits = sum(1 for _ in range(clicks) if rng.random() < 0.87)
                    if visits:
                        self.s.add(WebStat(date=d, utm_source="yandex", utm_medium="cpc", utm_campaign=str(c.id),
                                           utm_content=f"context|{p}", placement=p, visits=visits,
                                           bounces=sum(1 for _ in range(visits) if rng.random() < bounce),
                                           duration_sum=int(visits * dur * rng.uniform(0.8, 1.2)), goal_reaches=0))
                    self._leads(d, c, p, clicks * c.conv * conv_m * rng.uniform(0.7, 1.3), c.book * min(1.3, max(0.15, conv_m)))
        # Не из Директа: SEO, соцсети, прямые.
        self._other_leads(d, "yandex", "organic", 0.9 * k, 0.42)
        self._other_leads(d, "google", "organic", 0.5 * k, 0.40)
        self._other_leads(d, "instagram", "social", 0.6 * k, 0.30)
        self._other_leads(d, "", "", 0.5 * k, 0.45)
        self._walk_in(d)
        self._panel_leads(d, k)

    def _search_queries(self, d: date, c: Campaign, clicks: int):
        qs = SEARCH_QUERIES.get(c.id, [])
        if not qs or not clicks:
            return
        weights = [1.0 / (i + 1) for i in range(len(qs))]
        for q, w in zip(qs, weights):
            if any(n in q for n in self.negatives[c.id]):
                continue
            junk = any(x in q for x in ("ваканси", "работа", "раскраска", "своими руками"))
            qc = _poisson(self.rng, clicks * w / sum(weights) * (0.25 if junk else 1))
            if qc:
                self.s.add(SearchQuery(date=d, campaign_id=c.id, campaign_name=c.name, ad_group_id=c.id * 10 + 1, query=q,
                                       impressions=int(qc / c.ctr), clicks=qc, cost=round(qc * c.cpc * self.rng.uniform(0.9, 1.1), 2)))

    def _leads(self, d: date, c: Campaign, placement: str | None, lam: float, book_p: float):
        for _ in range(_poisson(self.rng, lam)):
            term = self.rng.choice(c.keywords) if c.keywords else ""
            content = f"context|{placement}" if placement else "search|none"
            self._make_lead(d, "yandex", "cpc", str(c.id), content, term, book_p)

    def _other_leads(self, d: date, src: str, medium: str, lam: float, book_p: float):
        for _ in range(_poisson(self.rng, lam)):
            self._make_lead(d, src, medium, "", "", "", book_p)

    def _walk_in(self, d: date):
        # Иногда клиент пишет в Instagram и менеджер заводит лид в CRM руками — обращений нет.
        if self.rng.random() < 0.25:
            created = self.at(d)
            self._crm(self.phone(), created, 0.5, "instagram", "", "")

    # Заявки, которые администратор вписал в панель руками: меток нет, источник — тег «Как узнали».
    PANEL_TAGS = [("Знакомые", "Были на чужом празднике", 0.35, 0.55), ("Поиск и карты", "2ГИС", 0.25, 0.35),
                  ("Реклама", "Реклама на Ozon", 0.12, 0.25), ("Реклама", "Яндекс Директ", 0.15, 0.3),
                  ("Сайт и соцсети", "Instagram", 0.12, 0.3), ("Уже знают нас", "Приходили раньше", 0.12, 0.6)]

    def _panel_leads(self, d: date, k: float):
        rng = self.panel_rng
        for group, tag, lam, book_p in self.PANEL_TAGS:
            for _ in range(_poisson(rng, lam * k)):
                self.panel_seq += 1
                phone = f"+3749{self.panel_seq:07d}"
                created = datetime.combine(d, time(rng.randint(10, 21), rng.randint(0, 59)))
                booked = rng.random() < book_p
                if tag in ("2ГИС", "Instagram", "Яндекс Директ") and rng.random() < 0.5:
                    # позвонили на номер колтрекинга: Callibri знает канал, а метки звонка пустые
                    self.s.add(Touch(source="call", system="callibri", external_id=f"panel-call:demo-{self.panel_seq}",
                                     created_at=created - timedelta(minutes=3), phone=phone, duration_sec=rng.randint(60, 300),
                                     answered=True, manager=rng.choice(MANAGERS), extra={"callibri_channel": tag}))
                self.s.add(Touch(source="panel", system="zayavki", external_id=f"lead:demo-{self.panel_seq}", created_at=created,
                                 phone=phone, name=rng.choice(NAMES), manager=rng.choice(MANAGERS),
                                 extra={"tag": tag, "tag_group": group}))
                self.s.add(CrmRecord(entity="panel", external_id=f"demo-{self.panel_seq}", phone=phone, title=rng.choice(NAMES),
                                     status_raw="бронь оплачена" if booked else "думает", status="prepaid" if booked else "in_progress",
                                     amount=rng.choice([60000, 80000, 100000]) if booked else 0, created_at=created + timedelta(minutes=5),
                                     manager=rng.choice(MANAGERS)))

    def _make_lead(self, d: date, src, medium, campaign, content, term, book_p):
        rng = self.rng
        phone = self.phone()
        created = self.at(d)
        channel = rng.choices(["call", "form", "chat"], weights=[0.45, 0.35, 0.2])[0]
        name = rng.choice(NAMES)
        utm = dict(utm_source=src, utm_medium=medium, utm_campaign=campaign, utm_content=content, utm_term=term)
        answered = True
        if channel == "call":
            answered = rng.random() > 0.13
            self.s.add(Touch(source="call", system="callibri", external_id=self.ext(), created_at=created, phone=phone,
                             duration_sec=rng.randint(40, 420) if answered else 0, answered=answered,
                             manager=rng.choice(MANAGERS) if answered else None, **utm))
            if not answered and rng.random() < 0.5:  # перезвонили сами
                self.s.add(Touch(source="call", system="callibri", external_id=self.ext(), created_at=created + timedelta(minutes=rng.randint(3, 90)),
                                 phone=phone, duration_sec=rng.randint(60, 300), answered=True, manager=rng.choice(MANAGERS), **utm))
                answered = True
        elif channel == "form":
            self.s.add(Touch(source="form", system="site", external_id=self.ext(), created_at=created, phone=phone, name=name,
                             extra={"form": "Заявка на праздник", "child_age": rng.randint(5, 13)}, **utm))
        else:
            self.s.add(Touch(source="chat", system="callibri", external_id=self.ext(), created_at=created,
                             phone=phone if rng.random() < 0.85 else None, name=name,
                             response_sec=int(rng.lognormvariate(5.0, 0.9)), manager=rng.choice(MANAGERS), **utm))
        if rng.random() < 0.22:  # повторное обращение в течение пары дней
            self.s.add(Touch(source="call", system="callibri", external_id=self.ext(), created_at=created + timedelta(hours=rng.randint(2, 60)),
                             phone=phone, duration_sec=rng.randint(30, 240), answered=True, manager=rng.choice(MANAGERS), **utm))
        if answered or channel != "call":
            self._crm(phone, created + timedelta(minutes=rng.randint(1, 30)), book_p, src, campaign, content)

    def _crm(self, phone, created: datetime, book_p, src, campaign, content):
        rng = self.rng
        age_days = (self.end - created.date()).days
        party = created.date() + timedelta(days=rng.randint(4, 28))
        manager = rng.choice(MANAGERS)
        if rng.random() < 0.05:
            status, raw, entity = "spam", "JUNK", "lead"
        elif rng.random() < book_p:
            entity = "deal"
            if party < self.end:
                status, raw = "done", "WON"
            else:
                status, raw = rng.choice([("booked", "PREPAYMENT_INVOICE"), ("prepaid", "EXECUTING")])
        elif age_days <= 3:
            status, raw, entity = rng.choice([("new", "NEW", "lead"), ("in_progress", "IN_PROCESS", "lead")])
        else:
            status, raw, entity = ("lost", "LOSE", "deal") if rng.random() < 0.7 else ("in_progress", "IN_PROCESS", "lead")
        guests = rng.randint(6, 18)
        amount = guests * rng.choice([7000, 8000, 9000, 10000]) + rng.choice([0, 15000, 25000]) if status in ("booked", "prepaid", "done") else 0
        self.s.add(CrmRecord(entity=entity, external_id=self.ext(), phone=phone, title=f"День рождения · {rng.choice(NAMES)}",
                             status_raw=raw, status=status, amount=amount, created_at=created,
                             modified_at=created + timedelta(days=min(age_days, rng.randint(0, 5))), manager=manager,
                             party_date=party if status not in ("spam",) else None, guests=guests if entity == "deal" else None,
                             lost_reason=rng.choice(LOST_REASONS) if status == "lost" else None,
                             utm_source=src, utm_campaign=campaign, utm_content=content))

    # ---------------------------------------------------------- снимки ---
    def snapshot(self, d: date):
        offset = (d - self.start).days
        for c in CAMPAIGNS:
            if offset == 50 and c.id == 701001:
                self.budget[c.id] = 20000  # «прежний подрядчик» поднял бюджет
            if offset == 62 and c.id == 701002:
                self.negatives[c.id].add("своими руками")
            strategy = DEMO_STRATEGY.get(c.id) or {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE"},
                                                   "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}
            if c.id == 701003 and offset >= 68:
                strategy = {"Search": {"BiddingStrategyType": "SERVING_OFF"}, "Network": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS"}}
            self.s.add(CampaignSnapshot(date=d, campaign_id=c.id, campaign_name=c.name, data={
                "Name": c.name, "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED", "DailyBudget": self.budget[c.id],
                "ExcludedSites": sorted(self.excluded) if c.network == "AD_NETWORK" else [],
                "NegativeKeywords": sorted(self.negatives[c.id]), "BiddingStrategy": strategy}))
        self.s.flush()
        record_changes(self.s, d)

    def seo(self):
        for kw, start_pos, end_pos in SEO_KEYWORDS:
            for i in range(0, 90, 3):
                d = self.start + timedelta(days=i)
                if d > self.end:
                    break
                p = start_pos + (end_pos - start_pos) * (i / 89) + self.rng.uniform(-2, 2)
                self.s.add(SeoPosition(date=d, keyword=kw, **_seo_where(kw), position=max(1, int(round(p))),
                                       url="https://drive-kids.example/" + ("birthday" if "рожд" in kw else "")))
        last = self.end
        for kw, start_pos, end_pos in SEO_KEYWORDS:
            self.s.add(SeoPosition(date=last, keyword=kw, **_seo_where(kw), position=max(1, end_pos + self.rng.randint(-1, 1)),
                                   url="https://drive-kids.example/" + ("birthday" if "рожд" in kw else "")))

    # ------------------------------------------------- вкладка «Директ» ---
    def direct_extras(self):
        """Разметка запросов (ключ / автотаргетинг), конверсии Директа и срезы по устройствам и демографии.

        Отдельный генератор случайных чисел: остальные демо-данные не меняются от этих добавок.
        """
        rng = random.Random(4242)
        conv = {c.id: c.conv for c in CAMPAIGNS}
        by_id = {c.id: c for c in CAMPAIGNS}
        for q in self.s.query(SearchQuery):
            kws = by_id[q.campaign_id].keywords
            junk = any(x in q.query for x in ("ваканси", "работа", "раскраска", "своими руками"))
            q.criterion_type = "KEYWORD"
            q.criterion = max(kws, key=lambda k: len(set(k.split()) & set(q.query.split()))) if kws else None
            q.conversions = 0 if junk else _poisson(rng, q.clicks * conv[q.campaign_id] * 1.1)
        d = self.start
        while d <= self.end:
            k = _season(d, self.start)
            for cid, items in AUTOTARGETING.items():
                c = by_id[cid]
                for query, cat, m in items:
                    clicks = _poisson(rng, 1.3 * k)
                    if not clicks:
                        continue
                    self.s.add(SearchQuery(date=d, campaign_id=cid, campaign_name=c.name, ad_group_id=cid * 10 + 1, query=query,
                                           impressions=int(clicks / c.ctr), clicks=clicks, cost=round(clicks * c.cpc * rng.uniform(0.8, 1.1), 2),
                                           criterion_type="AUTOTARGETING", criterion="---autotargeting", targeting_category=cat,
                                           conversions=_poisson(rng, clicks * c.conv * m)))
            d += timedelta(days=1)
        totals = {}
        for st in self.s.query(AdStat):
            t = totals.setdefault((st.date, st.campaign_id), [st.campaign_name, 0, 0, 0.0])
            t[1] += st.impressions
            t[2] += st.clicks
            t[3] += float(st.cost)
        for (day, cid), (name, impr, clicks, cost) in totals.items():
            for slice_name, groups in (("device", DEVICES), ("demography", DEMOGRAPHY)):
                for value, (share, m) in groups.items():
                    cl = int(round(clicks * share * rng.uniform(0.85, 1.15)))
                    self.s.add(DirectSliceStat(date=day, campaign_id=cid, campaign_name=name, slice=slice_name, value=value,
                                               impressions=int(impr * share), clicks=cl, cost=round(cost * share * rng.uniform(0.9, 1.1), 2),
                                               conversions=_poisson(rng, cl * conv[cid] * m)))
        self.s.flush()

    # ---------------------------------------------------------- решения ---
    def owner_decisions(self, now: datetime):
        """Имитация владельца: отвечает на предложения через пару дней, большую часть одобряет."""
        applier = ActionApplier(self.s, mode="dry_run")
        for a in self.s.query(Action).filter(Action.status == "proposed", Action.created_at <= now - timedelta(hours=44)).all():
            if self.rng.random() < 0.75:
                applier.approve(a, by="Владелец (Telegram)")
                a.decided_at = a.applied_at = now
                a.eval_due = now.date() + timedelta(days=7)
            else:
                applier.reject(a, by="Владелец (Telegram)")
                a.decided_at = now

    def sync_from_actions(self):
        for a in self.s.query(Action).filter(Action.status == "applied"):
            if a.kind == "exclude_site":
                self.excluded.add(a.target)
            elif a.kind == "add_negative":
                for cid in a.campaign_ids:
                    self.negatives[int(cid)].add(a.target)

    def traffic(self):
        """Визиты по источникам для вкладки «Дашборд»: Яндекс Поиск растёт, Google проседает, Instagram подрастает."""
        rng = random.Random(4343)  # свой генератор — остальное демо не меняется
        span = max(1, (self.end - self.start).days)
        # источник, поисковик, соцсеть, визитов в день в начале, к концу периода (множитель)
        plan = [("organic", "yandex", "", 62, 1.45), ("organic", "google", "", 34, 0.78), ("organic", "", "", 3, 1.0),
                ("social", "", "instagram", 14, 1.35), ("social", "", "vkontakte", 4, 0.9), ("direct", "", "", 40, 1.05),
                ("referral", "", "", 9, 1.0), ("messenger", "", "", 6, 1.2), ("internal", "", "", 5, 1.0)]
        d = self.start
        while d <= self.end:
            t = (d - self.start).days / span
            weekend = 1.18 if d.weekday() >= 5 else 1.0
            for src, eng, net, base, mult in plan:
                visits = max(0, int(base * (1 + (mult - 1) * t) * weekend * rng.uniform(0.8, 1.2)))
                if visits:
                    self.s.add(TrafficStat(date=d, source=src, engine=eng, network=net, visits=visits,
                                           bounces=int(visits * rng.uniform(0.15, 0.35)), goal_reaches=_poisson(rng, visits * 0.012)))
            d += timedelta(days=1)

    # ---------------------------------------------------------- build ---
    def build(self, simulate_days: int = 14):
        sim_from = self.end - timedelta(days=simulate_days - 1)
        applier = ActionApplier(self.s, mode="dry_run")
        d = self.start
        while d <= self.end:
            if d >= sim_from:
                run_at = datetime.combine(d, time(7, 0))
                rebuild_leads(self.s)
                self.owner_decisions(run_at)
                run_rules(self.s, applier, day=d - timedelta(days=1), at=run_at)
                evaluate_due(self.s, today=d)
                self.sync_from_actions()
            self.snapshot(d)
            self.day(d)
            self.s.flush()
            d += timedelta(days=1)
        self.seo()
        self.traffic()
        self.direct_extras()
        rebuild_leads(self.s)
        today = self.end + timedelta(days=1)
        run_at = datetime.combine(today, time(7, 0))
        self.owner_decisions(run_at)
        run_rules(self.s, applier, day=self.end, at=run_at)
        evaluate_due(self.s, today=today)
        for name in ("direct", "metrika", "callibri", "bitrix24", "topvisor"):
            self.s.add(CollectorRun(started_at=datetime.combine(today, time(6, 0)), finished_at=datetime.combine(today, time(6, 2)),
                                    collector=name, date_from=self.end, date_to=self.end, status="ok", rows=0,
                                    message="демо-данные", duration_sec=1.0))
        self.s.flush()


def reset(session: Session):
    for model in (Touch, Lead, CrmRecord, Action, SettingsChange, CampaignSnapshot, AdStat, SearchQuery, WebStat, TrafficStat, SeoPosition,
                  ReportLog, CollectorRun, DirectSliceStat, MetrikaSourceStat):
        session.execute(delete(model))
    session.flush()


def build_demo(session: Session, end: date, days: int = 90, seed: int = 42):
    reset(session)
    DemoBuilder(session, end, days, seed).build()
    metrika_sources(session, seed)


# Как Метрика назвала бы источник заявки консоли: (тип трафика, система)
_METRIKA_NAMES = {"Яндекс Директ": ("Переходы по рекламе", "Яндекс: Директ"), "Поиск": ("Переходы из поисковых систем", "Яндекс"),
                  "Instagram": ("Переходы из социальных сетей", "Instagram"), "2ГИС": ("Переходы по ссылкам на сайтах", "2gis.ru"),
                  "Без источника": ("Прямые заходы", "")}


def metrika_sources(session: Session, seed: int = 42):
    """Цель «заявка» в Метрике по источникам: заявки с сайта ± шум, чтобы сверка показала и совпадение, и потери."""
    rng = random.Random(seed + 2)
    session.execute(delete(MetrikaSourceStat))
    leads = session.query(Lead).filter(Lead.channel.in_(("call", "form", "chat", "site_form")), Lead.status != "spam").all()
    per: dict[tuple, int] = {}
    for lead in leads:
        if lead.source_channel in _METRIKA_NAMES and not (lead.source_channel == "2ГИС" and lead.source_method == "call"):
            key = (lead.first_touch_at.date(), lead.source_channel)
            per[key] = per.get(key, 0) + 1
    for (d, ch), n in per.items():
        goals = n + (1 if rng.random() < 0.08 else 0)
        if ch == "Instagram" and rng.random() < 0.5:  # в демо часть заявок из Instagram «теряется» по дороге в консоль
            goals += 1
        traffic, engine = _METRIKA_NAMES[ch]
        session.add(MetrikaSourceStat(date=d, traffic_source=traffic, source_engine=engine, visits=goals * rng.randint(8, 14),
                                      goal_reaches=goals))
    for lead in leads:  # ClientID есть у большинства заявок с сайта
        if rng.random() < 0.8:
            session.query(Touch).filter(Touch.lead_id == lead.id).update({"ym_client_id": str(10**15 + lead.id)})
    session.flush()
