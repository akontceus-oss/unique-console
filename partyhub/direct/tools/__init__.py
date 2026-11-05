"""Все модули этой папки подключаются автоматически: новый инструмент = новый файл с @register."""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_m.name}")
