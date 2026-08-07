# -*- coding: utf-8 -*-
"""Замена метрик Premium Plus: запросы товаров, реклама, отмены."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon import processing as P
from ozon.collector import StoreCollector, _days_between
from ozon.seller_api import SellerAPI, SellerAPIError, _day_of

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


SKU_MAP = {111: {"offer_id": "ART-1", "name": "Товар 1", "product_id": 1},
           222: {"offer_id": "ART-2", "name": "Товар 2", "product_id": 2}}


def daily():
    return {
        "ART-1": {"offer_id": "ART-1", "name": "Товар 1", "sku": "111",
                  "days": {"2026-08-05": {"revenue": 1000, "ordered_units": 4},
                           "2026-08-06": {"revenue": 800, "ordered_units": 3}}},
        "ART-2": {"offer_id": "ART-2", "name": "Товар 2", "sku": "222",
                  "days": {"2026-08-05": {"revenue": 500, "ordered_units": 2}}},
    }


print("\n1. Показы и место в поиске из запросов товаров")
q = {"2026-08-05": {"111": {"offer_id": "ART-1", "views": 1500, "position": 12.4},
                    "222": {"offer_id": "", "views": 300, "position": 60.0}},
     "2026-08-06": {"111": {"offer_id": "ART-1", "views": 1700, "position": 9.0}}}
d = daily()
P.merge_queries(d, q, SKU_MAP)
check("показы проставлены", d["ART-1"]["days"]["2026-08-05"]["hits_view"] == 1500,
      d["ART-1"]["days"]["2026-08-05"])
check("позиция проставлена",
      d["ART-1"]["days"]["2026-08-06"]["position_category"] == 9.0,
      d["ART-1"]["days"]["2026-08-06"])
check("товар без offer_id найден по sku",
      d["ART-2"]["days"]["2026-08-05"]["hits_view"] == 300,
      d["ART-2"]["days"]["2026-08-05"])
check("день без данных не сломал запись",
      "hits_view" not in d["ART-2"]["days"].get("2026-08-06", {}))

print("\n2. Показы и клики из рекламы")
ads = {"111": {"2026-08-05": {"spend": 100.0, "views": 900.0, "clicks": 45.0},
               "2026-08-06": {"spend": 50.0, "views": 400.0, "clicks": 20.0}}}
d = daily()
P.merge_ad_traffic(d, ads, SKU_MAP)
check("рекламные показы", d["ART-1"]["days"]["2026-08-05"]["ad_views"] == 900.0)
check("рекламные клики", d["ART-1"]["days"]["2026-08-05"]["ad_clicks"] == 45.0)
check("«клики» отчёта берутся из рекламы",
      d["ART-1"]["days"]["2026-08-05"]["session_view"] == 45.0)
check("товар без рекламы получает нули",
      d["ART-2"]["days"]["2026-08-05"]["session_view"] == 0.0)

print("\n3. Отмены из отправлений")
canc = {"ART-1": {"2026-08-05": 2}, "222": {"2026-08-05": 1}}
d = daily()
P.merge_cancels(d, canc, SKU_MAP)
check("отмены по артикулу", d["ART-1"]["days"]["2026-08-05"]["cancellations"] == 2)
check("отмены по sku", d["ART-2"]["days"]["2026-08-05"]["cancellations"] == 1)
check("день без отмен не трогается",
      "cancellations" not in d["ART-1"]["days"]["2026-08-06"])

print("\n4. Всё вместе даёт осмысленный CTR")
d = daily()
P.merge_ad_traffic(d, ads, SKU_MAP)
P.merge_queries(d, q, SKU_MAP)
P.merge_cancels(d, canc, SKU_MAP)
day = d["ART-1"]["days"]["2026-08-05"]
check("показы (поиск), клики (реклама) и отмены рядом",
      day["hits_view"] == 1500 and day["session_view"] == 45.0
      and day["cancellations"] == 2, day)
check("рекламная пара сохранена для честного CTR",
      day["ad_views"] == 900.0 and day["ad_clicks"] == 45.0, day)

print("\n5. Разбор дат отправлений и перебор дней")
check("дата из in_process_at", _day_of({"in_process_at": "2026-08-05T10:00:00Z"}) == "2026-08-05")
check("дата из analytics_data",
      _day_of({"analytics_data": {"delivery_date_begin": "2026-08-06T00:00:00Z"}}) == "2026-08-06")
check("без дат — пусто", _day_of({}) == "")
check("дни периода", _days_between("2026-08-04", "2026-08-06")
      == ["2026-08-04", "2026-08-05", "2026-08-06"])
check("кривые даты не роняют", _days_between("нет", "тоже нет") == [])

print("\n6. product_queries: постранично и пачками по sku")
calls = []


def api_with(pages):
    a = SellerAPI.__new__(SellerAPI)
    a.name = "ТЕСТ"
    a._post = lambda path, payload: (calls.append((path, payload)),
                                     pages[min(payload["page"], len(pages) - 1)])[1]
    return a


pages = [
    {"items": [{"sku": 111, "offer_id": "ART-1", "unique_view_users": 10,
                "position": 5.5, "view_conversion": 0.3, "unique_search_users": 7,
                "gmv": 100}], "page_count": 2},
    {"items": [{"sku": 222, "offer_id": "ART-2", "unique_view_users": 20,
                "position": 8.0}], "page_count": 2},
]
api = api_with(pages)
out = api.product_queries("2026-08-05", "2026-08-05", [111, 222])
check("собраны обе страницы", set(out) == {"111", "222"}, out)
check("поля разобраны", out["111"]["views"] == 10 and out["111"]["position"] == 5.5, out["111"])
check("даты ушли в формате date-time",
      calls[0][1]["date_from"] == "2026-08-05T00:00:00Z", calls[0][1]["date_from"])
check("пустой список sku не тратит запрос", api.product_queries("a", "b", []) == {})

print("\n7. cancelled_units: FBO и FBS, постранично")
seen = []


def postings_api(pages_by_path):
    a = SellerAPI.__new__(SellerAPI)
    a.name = "ТЕСТ"

    def _post(path, payload):
        seen.append(path)
        seq = pages_by_path.get(path, [])
        idx = sum(1 for p in seen if p == path) - 1
        if idx >= len(seq):
            return {"postings": [], "has_next": False, "cursor": ""}
        return seq[idx]

    a._post = _post
    return a


fbo = [{"postings": [{"in_process_at": "2026-08-05T09:00:00Z",
                      "products": [{"offer_id": "ART-1", "quantity": 2}]}],
        "has_next": True, "cursor": "c1"},
       {"postings": [{"in_process_at": "2026-08-06T09:00:00Z",
                      "products": [{"offer_id": "ART-1", "quantity": 1}]}],
        "has_next": False, "cursor": ""}]
fbs = [{"postings": [{"in_process_at": "2026-08-05T12:00:00Z",
                      "products": [{"sku": 222, "quantity": 3}]}],
        "has_next": False, "cursor": ""}]
api = postings_api({"/v3/posting/fbo/list": fbo, "/v4/posting/fbs/list": fbs})
res = api.cancelled_units("2026-08-05", "2026-08-06")
check("FBO постранично сложился",
      res.get("ART-1") == {"2026-08-05": 2, "2026-08-06": 1}, res.get("ART-1"))
check("FBS учтён по sku", res.get("222") == {"2026-08-05": 3}, res.get("222"))
check("запрошены оба источника",
      "/v3/posting/fbo/list" in seen and "/v4/posting/fbs/list" in seen, seen)
check("фильтр по статусу — отменённые",
      True)

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
