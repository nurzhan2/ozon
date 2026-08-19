# -*- coding: utf-8 -*-
"""
Выгрузке кабинета передаются настройки СЕРВИСА, а не словарь магазина.

Ошибка, из-за которой импорт не работал ни дня: сбор вызывал
CAB.load(self.name, self.cfg), где self.cfg — словарь одного магазина из
OZON_STORES. Ни GOOGLE_IMPORT_FOLDER, ни DATA_DIR в нём нет и быть не может,
getattr молча возвращал пустую строку, и в логе стояло «GOOGLE_IMPORT_FOLDER
не задан» — при заданной в Railway переменной. Диагностика заняла две недели
переписки с заказчиком.
"""
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OZON_STORES",
                      '[{"name":"ТЕСТ","client_id":"1","api_key":"k"}]')
os.environ.setdefault("DATA_DIR", "/tmp/_wiring")

from ozon import cabinet as CAB
from ozon.collector import StoreCollector

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


STORE = {"name": "ТЕСТ", "client_id": "1", "api_key": "k"}


class AppCfg:
    """Как настоящий модуль config."""
    DATA_DIR = "/tmp/_wiring"
    GOOGLE_IMPORT_FOLDER = "1abcDEF"
    GOOGLE_CREDENTIALS_FILE = "/tmp/_wiring/key.json"
    TIMEZONE = "Europe/Moscow"


class Rec(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, r):
        self.lines.append(r.getMessage())


def catch(fn):
    h = Rec()
    for name in ("ozon.cabinet", "ozon.collector"):
        logging.getLogger(name).addHandler(h)
        logging.getLogger(name).setLevel(logging.INFO)
    try:
        out = fn()
    finally:
        for name in ("ozon.cabinet", "ozon.collector"):
            logging.getLogger(name).removeHandler(h)
    return out, "\n".join(h.lines)


shutil.rmtree("/tmp/_wiring", ignore_errors=True)
os.makedirs("/tmp/_wiring", exist_ok=True)

print("\n1. Сборщик держит настройки сервиса отдельно от настроек магазина")
c = StoreCollector.__new__(StoreCollector)
c.cfg, c.app_cfg, c.name = STORE, AppCfg, "ТЕСТ"
check("словарь магазина остался в cfg", c.cfg["client_id"] == "1", c.cfg)
check("настройки сервиса — в app_cfg",
      c._settings().GOOGLE_IMPORT_FOLDER == "1abcDEF", c._settings())

print("\n2. Без app_cfg сборщик сам находит модуль config")
c2 = StoreCollector.__new__(StoreCollector)
c2.cfg, c2.app_cfg, c2.name = STORE, None, "ТЕСТ"
got = c2._settings()
check("модуль config подтянулся", hasattr(got, "DATA_DIR"), got)
check("и запомнился, чтобы не импортировать каждый раз",
      c2.app_cfg is got, c2.app_cfg)

print("\n3. Словарь магазина вместо настроек — громкая ошибка, а не тишина")
out, text = catch(lambda: CAB.load("ТЕСТ", STORE))
check("это ошибка уровня ERROR, её видно в Railway",
      "настройки магазина" in text, text)
check("сказано, что виноват код, а не переменные",
      "ошибка в коде" in text, text)
check("возвращается правильная форма", out == {"metrics": {}, "orders": {}}, out)
check("НЕ пишется вводящее в заблуждение «GOOGLE_IMPORT_FOLDER не задан»",
      "не задан" not in text, text)

print("\n4. None вместо настроек тоже не молчит")
out, text = catch(lambda: CAB.load("ТЕСТ", None))
check("предупреждение есть", "настроек сервиса нет" in text, text)
check("форма правильная", out == {"metrics": {}, "orders": {}}, out)

print("\n5. Настоящие настройки читаются как надо")
out, text = catch(lambda: CAB.load("ТЕСТ", AppCfg))
check("папка увидена — про «не задан» речи нет", "не задан" not in text, text)
check("ключа нет, и об этом сказано",
      "ключа сервисного аккаунта нет" in text, text)

print("\n6. Точка входа передаёт настройки явно")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "worker.py"), encoding="utf-8").read()
check("worker.py передаёт app_cfg", "app_cfg=config" in src,
      [l for l in src.splitlines() if "StoreCollector" in l])
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "run.py"), encoding="utf-8").read()
check("run.py передаёт app_cfg", "app_cfg=config" in src,
      [l for l in src.splitlines() if "app_cfg" in l])

print("\n7. Сбор не лезет за папкой в словарь магазина")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ozon", "collector.py"), encoding="utf-8").read()
code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
check("вызова CAB.load(self.name, self.cfg) больше нет",
      not any("CAB.load(self.name, self.cfg)" in l for l in code),
      [l for l in code if "CAB.load" in l])

shutil.rmtree("/tmp/_wiring", ignore_errors=True)

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
