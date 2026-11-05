"""Инструменты вкладки «Директ»: общий интерфейс и реестр.

Инструмент — один файл в partyhub/direct/tools/. Он анализирует данные из базы и возвращает
таблицу для вкладки и, если нужно, предложения правок (Proposal). Вкладка строится по описанию
инструмента, поэтому новый инструмент появляется в интерфейсе без правки HTML:

    @register
    class MyTool(DirectTool):
        id = "my_tool"
        title = "Мой инструмент"
        group = "Чистка"
        description = "Что делает и на каких данных."
        campaign_types = ("search",)
        columns = [Col("campaign", "Кампания"), Col("cost", "Расход", "money")]

        def run(self, ctx: ToolContext) -> ToolResult:
            ...
            return ToolResult(rows=rows, proposals=proposals)

Настройки инструмента берутся из config/rules.yaml → direct_tools.<id>.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..config import load_rules
from ..rules.engine import Proposal

# Типы кампаний для фильтра вкладки.
CAMPAIGN_TYPES = {
    "search": "Поиск",
    "network": "РСЯ",
    "autotargeting": "Автотаргетинг",
    "unified": "ЕПК",
}
# Правки, которые инструмент может предложить. Автоматически (без «да» человека) применяются только
# исключение площадок и минус-слова — и только через утренний цикл правил с его лимитами.
KINDS = {"exclude_site", "add_negative", "bid_modifier", "set_cpa", "alert"}


@dataclass
class Col:
    key: str
    label: str
    fmt: str = "text"  # text | money | int | pct | num | chip | long
    wide: bool = False  # прятать на узком экране


@dataclass
class ToolContext:
    session: Session
    d1: date
    d2: date
    params: dict = field(default_factory=dict)

    @property
    def cfg(self) -> dict:
        return self.params.get("_cfg", {})


@dataclass
class ToolResult:
    rows: list[dict] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    summary: list[dict] = field(default_factory=list)  # [{"label", "value", "fmt", "tone"}]
    notes: list[str] = field(default_factory=list)


class DirectTool:
    id = "base"
    title = "—"
    group = "Прочее"
    description = ""
    campaign_types: tuple[str, ...] = tuple(CAMPAIGN_TYPES)
    columns: list[Col] = []
    order = 100
    can_propose = True  # есть ли кнопка «Отправить в журнал»
    uses_ai = False

    def config(self) -> dict:
        return (load_rules().get("direct_tools") or {}).get(self.id) or {}

    def enabled(self) -> bool:
        return self.config().get("enabled", True)

    def run(self, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def manifest(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "group": self.group,
            "description": self.description,
            "campaign_types": list(self.campaign_types),
            "columns": [asdict(c) for c in self.columns],
            "can_propose": self.can_propose,
            "uses_ai": self.uses_ai,
            "daily": bool(self.config().get("daily")),
            "level": self.config().get("level", "approval"),
        }


TOOLS: dict[str, DirectTool] = {}


def register(cls: type[DirectTool]) -> type[DirectTool]:
    TOOLS[cls.id] = cls()
    return cls


def get_tool(tool_id: str) -> DirectTool:
    from . import tools  # noqa: F401  регистрирует все инструменты

    if tool_id not in TOOLS:
        raise KeyError(tool_id)
    return TOOLS[tool_id]


def all_tools() -> list[DirectTool]:
    from . import tools  # noqa: F401

    return sorted((t for t in TOOLS.values() if t.enabled()), key=lambda t: (t.order, t.title))
