# -*- coding: utf-8 -*-
"""
Когда /v1/analytics/stocks отбивает 429 все семь попыток.

Заказчик: «в отчёте остатков магазин Бф стали подгружать наименования
складов, а не кластер». Так и было: запасной путь подставлял имя склада
в колонку «Кластер», и вместо «Москва, МО и Дальние регионы» появлялись
УФА_РФЦ, ПУШКИНО_2_РФЦ и ещё три десятка строк на товар.

Остатки при этом правильные — ломается только группировка. Значит её надо
взять из прошлого удачного прогона: карта «склад -> кластер» меняется редко.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = "/tmp/_clfallback/cache"
os.environ["PERF_CACHE_DIR"] = CACHE
os.environ.setdefault("DATA_DIR", "/tmp/_clfallback")

from ozon import collector as C
from ozon.collector import StoreCollector
from ozon.seller_api import SellerAPIError

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


# Как OZON отдаёт кластерный ответ: строка на СКЛАД, кластер повторяется.
CLUSTER_ROWS = [
    {"offer_id": "ART-1", "name": "Гриб", "cluster": "Москва, МО и Дальние регионы",
     "warehouse": "ПУШКИНО_2_РФЦ", "available": 1369, "requested": 0, "transit": 0,
     "ads": 62.0, "idc": 22.0, "ads_all": 90.0},
    {"offer_id": "ART-1", "name": "Гриб", "cluster": "Москва, МО и Дальние регионы",
     "warehouse": "ВАТУТИНКИ_РФЦ", "available": 1071, "requested": 0, "transit": 0,
     "ads": 62.0, "idc": 22.0, "ads_all": 90.0},
    {"offer_id": "ART-1", "name": "Гриб", "cluster": "Урал", "warehouse": "УФА_РФЦ",
     "available": 1384, "requested": 0, "transit": 0,
     "ads": 40.6, "idc": 34.0, "ads_all": 90.0},
]

# А так — складской: тот же товар, но кластера в ответе нет вовсе.
WAREHOUSE_ROWS = [
    {"offer_id": "ART-1", "sku": 1, "name": "Гриб", "cluster": "ПУШКИНО_2_РФЦ",
     "warehouse": "ПУШКИНО_2_РФЦ", "available": 1300, "requested": 0,
     "transit": 0, "ads": 0.0, "idc": 0.0},
    {"offer_id": "ART-1", "sku": 1, "name": "Гриб", "cluster": "ВАТУТИНКИ_РФЦ",
     "warehouse": "ВАТУТИНКИ_РФЦ", "available": 1000, "requested": 50,
     "transit": 0, "ads": 0.0, "idc": 0.0},
    {"offer_id": "ART-1", "sku": 1, "name": "Гриб", "cluster": "УФА_РФЦ",
     "warehouse": "УФА_РФЦ", "available": 1384, "requested": 0,
     "transit": 0, "ads": 0.0, "idc": 0.0},
    {"offer_id": "ART-1", "sku": 1, "name": "Гриб", "cluster": "НОВЫЙ_РФЦ",
     "warehouse": "НОВЫЙ_РФЦ", "available": 7, "requested": 0,
     "transit": 0, "ads": 0.0, "idc": 0.0},
]


class Seller:
    def __init__(self, cluster_ok=True):
        self.cluster_ok = cluster_ok
        self.warehouse_calls = 0

    def cluster_stocks(self, skus=None):
        if not self.cluster_ok:
            raise SellerAPIError("/v1/analytics/stocks HTTP 429 (попытка 7/7)")
        return [dict(r) for r in CLUSTER_ROWS]

    def stocks_on_warehouses(self, limit=1000):
        self.warehouse_calls += 1
        return [dict(r) for r in WAREHOUSE_ROWS]


def make(cluster_ok):
    c = StoreCollector.__new__(StoreCollector)
    c.name = "БЬЮТИФУЛ"
    c.exclude_marker = "OUT"
    c.seller = Seller(cluster_ok)
    c._cluster_rows = None
    c._sku_map = {1: {"offer_id": "ART-1"}}
    c._offer_map = {}
    c.maps = lambda: (c._sku_map, c._offer_map)
    c.offer_names = lambda: {"ART-1": "Гриб"}
    return c


shutil.rmtree("/tmp/_clfallback", ignore_errors=True)

print("\n1. Пока карты нет, запасной путь честно отдаёт склады")
c = make(cluster_ok=False)
rows = c.cluster_stocks()
check("склады запрошены", c.seller.warehouse_calls == 1, c.seller.warehouse_calls)
check("в колонке кластера пока имена складов",
      {r["cluster"] for r in rows} == {"ПУШКИНО_2_РФЦ", "ВАТУТИНКИ_РФЦ",
                                       "УФА_РФЦ", "НОВЫЙ_РФЦ"},
      {r["cluster"] for r in rows})

print("\n2. Удачный прогон сохраняет карту «склад -> кластер»")
c = make(cluster_ok=True)
rows = c.cluster_stocks()
check("вернулись кластерные строки",
      {r["cluster"] for r in rows} == {"Москва, МО и Дальние регионы", "Урал"},
      {r["cluster"] for r in rows})
path = C._cluster_map_path("БЬЮТИФУЛ")
check("файл карты появился", os.path.exists(path), path)
cache = C._read_json(path, {})
check("склады записаны",
      cache["warehouses"]["УФА_РФЦ"] == "Урал"
      and cache["warehouses"]["ПУШКИНО_2_РФЦ"] == "Москва, МО и Дальние регионы",
      cache.get("warehouses"))
check("ads_cluster тоже сохранён",
      cache["ads"]["ART-1\tУрал"][0] == 40.6, cache.get("ads"))

print("\n3. Следующий прогон упал — кластеры берутся из карты")
c = make(cluster_ok=False)
rows = c.cluster_stocks()
by_cluster = {r["cluster"]: r for r in rows}
check("имён складов в колонке кластера больше нет",
      "УФА_РФЦ" not in by_cluster and "ПУШКИНО_2_РФЦ" not in by_cluster,
      sorted(by_cluster))
check("Москва собралась из двух складов",
      "Москва, МО и Дальние регионы" in by_cluster, sorted(by_cluster))
msk = by_cluster["Москва, МО и Дальние регионы"]
check("остатки складов сложены", msk["available"] == 2300, msk["available"])
check("заявки тоже", msk["requested"] == 50, msk["requested"])
check("Урал остался одной строкой",
      by_cluster["Урал"]["available"] == 1384, by_cluster["Урал"])

print("\n4. Остатки берутся свежие, а не из кэша")
check("Москва не 1369+1071 из старого кластерного ответа, а 1300+1000",
      msk["available"] != 2440, msk["available"])

print("\n5. ads_cluster восстановлен — есть от чего считать потребность")
check("Москва", msk["ads"] == 62.0, msk["ads"])
check("Урал", by_cluster["Урал"]["ads"] == 40.6, by_cluster["Урал"]["ads"])
check("idc тоже", msk["idc"] == 22.0, msk["idc"])

print("\n6. Незнакомый склад не теряется")
check("НОВЫЙ_РФЦ остался отдельной строкой под своим именем",
      "НОВЫЙ_РФЦ" in by_cluster, sorted(by_cluster))
check("и его остаток на месте", by_cluster["НОВЫЙ_РФЦ"]["available"] == 7,
      by_cluster["НОВЫЙ_РФЦ"])

print("\n7. Карта переживает перезапуск и не портится складским прогоном")
c = make(cluster_ok=False)
c.cluster_stocks()
cache2 = C._read_json(C._cluster_map_path("БЬЮТИФУЛ"), {})
check("после неудачного прогона карта та же",
      cache2.get("warehouses") == cache.get("warehouses"), cache2.get("warehouses"))

print("\n8. Карта у каждого магазина своя")
check("имя магазина попало в имя файла",
      C._cluster_map_path("БЬЮТИФУЛ") != C._cluster_map_path("ШТУЧКА"),
      C._cluster_map_path("ШТУЧКА"))
check("кириллица в имени файла не ломает путь",
      C._cluster_map_path("ЛИНИЯ МЕЧТЫ").endswith(".json"),
      C._cluster_map_path("ЛИНИЯ МЕЧТЫ"))

shutil.rmtree("/tmp/_clfallback", ignore_errors=True)

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
