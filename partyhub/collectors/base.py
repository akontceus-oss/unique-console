"""Общий интерфейс сборщика и запуск с записью в журнал."""
from __future__ import annotations

import time
import traceback
from datetime import date

from sqlalchemy.orm import Session

from ..models import CollectorRun
from ..pipeline.normalize import now_local


class Collector:
    """Сборщик забирает данные источника за период и перезаписывает этот период в базе.

    Перезапись делает сбор идемпотентным: повторный запуск за те же дни не создаёт дублей,
    а поздние правки источника (например, сменившийся статус звонка) подтягиваются.
    """

    name = "base"

    def configured(self) -> bool:
        return True

    def collect(self, session: Session, date_from: date, date_to: date) -> int:
        raise NotImplementedError


def run_collector(session: Session, collector: Collector, date_from: date, date_to: date) -> CollectorRun:
    run = CollectorRun(
        started_at=now_local(), collector=collector.name, date_from=date_from, date_to=date_to, status="running", rows=0
    )
    session.add(run)
    session.flush()
    t0 = time.monotonic()
    if not collector.configured():
        run.status, run.message = "skipped", "не настроено: заполните подключение во вкладке «Подключения»"
    else:
        try:
            with session.begin_nested():
                run.rows = collector.collect(session, date_from, date_to)
            run.status = "ok"
            if getattr(collector, "warning", ""):
                run.message = collector.warning
        except Exception as e:  # сбой одного источника не должен останавливать остальные
            run.status = "error"
            run.message = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"[:4000]
    run.finished_at = now_local()
    run.duration_sec = round(time.monotonic() - t0, 2)
    session.flush()
    return run
