import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Каждый тест — с чистой базой SQLite и предсказуемыми настройками."""
    monkeypatch.chdir(tmp_path)  # чтобы не подхватить .env из корня проекта
    db = tmp_path / "test.db"
    values = {
        "DATABASE_URL": f"sqlite:///{db}",
        "TIMEZONE": "Asia/Yerevan",
        "PHONE_REGION": "AM",
        "CURRENCY_SYMBOL": "֏",
        "DIRECT_MODE": "dry_run",
        "DASHBOARD_USER": "admin",
        "DASHBOARD_PASSWORD": "secret",
        "WEBHOOK_TOKEN": "hook-token-1",
        "TELEGRAM_WEBHOOK_SECRET": "tgsecret",
        "TELEGRAM_CHAT_ID": "42",
        "TELEGRAM_BOT_TOKEN": "",
        "AI_PROVIDER": "template",
        "RULES_FILE": str(ROOT / "config" / "rules.yaml"),
        "PLATFORM_SECRET_KEY": "test-platform-key",
        "PLATFORM_DATABASE_URL": "",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    from partyhub import config

    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    config.get_settings.cache_clear()
    config.load_rules.cache_clear()
    from partyhub.db import create_all, init_engine

    from partyhub.projects import reset_platform

    reset_platform()
    init_engine(values["DATABASE_URL"])
    create_all()
    yield
    config.get_settings.cache_clear()
    config.load_rules.cache_clear()


@pytest.fixture
def session():
    from partyhub.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def fixed_now(monkeypatch):
    """Замораживает «сейчас» платформы: 21.10.2026 07:00."""
    now = datetime(2026, 10, 21, 7, 0)
    import partyhub.pipeline.normalize as n

    monkeypatch.setattr(n, "now_local", lambda: now)
    monkeypatch.setattr(n, "today_local", lambda: now.date())
    monkeypatch.setattr(n, "yesterday", lambda: date(2026, 10, 20))
    for mod in ("partyhub.rules.engine", "partyhub.rules.actions", "partyhub.pipeline.metrics", "partyhub.reports.daily",
                "partyhub.web.app", "partyhub.jobs"):
        m = sys.modules.get(mod) or __import__(mod, fromlist=["x"])
        for name, val in (("now_local", lambda: now), ("today_local", lambda: now.date()), ("yesterday", lambda: date(2026, 10, 20))):
            if hasattr(m, name):
                monkeypatch.setattr(m, name, val)
    return now
