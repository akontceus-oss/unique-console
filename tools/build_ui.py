"""Собирает веб-интерфейс платформы из общих частей.

partyhub/web/ui/body.html  — разметка, стили и скрипты дашборда (общие с прототипом)
partyhub/web/ui/shell.html — оболочка платформы: цвета, d3 и адаптер `dash` поверх API
partyhub/web/ui/direct.html — вкладка «Директ» (работает только в платформе: ей нужен API)
partyhub/web/ui/connections.html — вкладка «Подключения» проекта (только в платформе)
→ partyhub/web/static/index.html; список проектов — partyhub/web/static/projects.html (отдельная страница)

Запуск: python tools/build_ui.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "partyhub" / "web" / "ui"
OUT = ROOT / "partyhub" / "web" / "static" / "index.html"

body = (UI / "body.html").read_text(encoding="utf-8")
body = body.replace("<!--DIRECT-->", (UI / "direct.html").read_text(encoding="utf-8"))
body = body.replace("<!--CONNECTIONS-TAB-->", '<button class="ph-tab" role="tab" data-tab="connections" aria-selected="false">Подключения</button>')
body = body.replace("<!--CONNECTIONS-->", '  <section class="ph-page" id="connections" role="tabpanel" aria-label="Подключения" hidden>\n'
                    + (UI / "connections.html").read_text(encoding="utf-8") + "  </section>")
shell = (UI / "shell.html").read_text(encoding="utf-8")
OUT.write_text(shell.replace("<!--BODY-->", body), encoding="utf-8")
print(f"{OUT.relative_to(ROOT)}: {OUT.stat().st_size // 1024} КБ")
