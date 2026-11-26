"""Поиск и откат «перекрёстных» сохранений (ошибка автосохранения карточки до 26.11.2026).

Ошибка: после открытия нескольких карточек подряд одно изменение сохранялось и в открытую заявку,
и во все открытые раньше — их поля перезаписывались полями открытой. Признак в истории:
один пользователь за 3 секунды поменял несколько заявок. «Настоящая» заявка в такой группе —
та, где поменялось меньше всего полей; остальные — пострадавшие.

Запуск в контейнере панели:
  python -m tools.fix_crossed           — только отчёт (без имён и телефонов), база не меняется
  python -m tools.fix_crossed --apply   — вернуть старые значения пострадавшим заявкам
Поле возвращается, только если с тех пор его никто не менял. Каждый откат пишется в историю
заявки от имени «система».
"""
import sys
from datetime import timedelta

from sqlalchemy import select

from app.db import session_scope
from app.main import apply_changes, now
from app.models import Lead, LeadEvent
from app.phones import format_phone

WINDOW = timedelta(seconds=3)
SKIP = {"created", "merged", "deleted", "restored"}


def clusters(s):
    evs = s.scalars(select(LeadEvent).where(LeadEvent.user_id.is_not(None)).order_by(LeadEvent.user_id, LeadEvent.at, LeadEvent.id)).all()
    out, cur = [], []
    for e in evs:
        if e.field in SKIP:
            continue
        if cur and (e.user_id != cur[-1].user_id or e.at - cur[-1].at > WINDOW):
            out.append(cur)
            cur = []
        cur.append(e)
    if cur:
        out.append(cur)
    return [c for c in out if len({e.lead_id for e in c}) > 1]


def current(lead, field):
    if field == "phone":
        return lead.phone_norm or ""
    v = getattr(lead, field, None)
    return "" if v is None else str(v)


def main(apply: bool):
    with session_scope() as s:
        groups = clusters(s)
        fixed = manual = 0
        for c in sorted(groups, key=lambda c: c[0].at, reverse=True):     # откат — от последних к первым
            by_lead = {}
            for e in c:
                by_lead.setdefault(e.lead_id, []).append(e)
            sizes = sorted(len({e.field for e in v}) for v in by_lead.values())
            main_id = min(by_lead, key=lambda i: len({e.field for e in by_lead[i]}))
            tie = sizes[0] == sizes[1]
            who = c[0].user.name if c[0].user else c[0].user_id
            print(f"\n{c[0].at:%d.%m %H:%M:%S} · {who} · заявок {len(by_lead)}" + (" · НЕ ЯСНО, какая настоящая — проверить вручную" if tie else ""))
            for lid, evs in by_lead.items():
                lead = s.get(Lead, lid)
                tag = "настоящая" if lid == main_id and not tie else "пострадала"
                print(f"   заявка id {lid} (№{lead.day_no} от {lead.created_at:%d.%m}) — {tag}: {', '.join(sorted({e.field for e in evs}))}")
                if tie or lid == main_id:
                    continue
                restore = {}
                for e in sorted(evs, key=lambda e: e.id, reverse=True):
                    if current(lead, e.field) == e.old:
                        continue                                  # уже старое значение
                    if current(lead, e.field) == e.new:
                        restore[e.field] = format_phone(e.old) if e.field == "phone" and e.old else e.old
                    else:
                        print(f"      {e.field}: с тех пор меняли — не трогаю")
                if apply and restore:
                    for f, v in restore.items():          # по одному: так у каждого поля своя строка истории
                        apply_changes(s, lead, {f: v}, None, now())
                    fixed += 1
            manual += tie
        print(f"\nГрупп: {len(groups)}. " + (f"Восстановлено заявок: {fixed}." if apply else "Это отчёт, база не менялась.") + (f" Проверить вручную: {manual}." if manual else ""))


if __name__ == "__main__":
    main("--apply" in sys.argv)
