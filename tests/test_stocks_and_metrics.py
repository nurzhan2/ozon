# -*- coding: utf-8 -*-
"""Остатки в новом формате OZON и устаревшие метрики аналитики."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ozon.seller_api as S
from ozon.seller_api import SellerAPI, SellerAPIError
from ozon import processing as P

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


def api_with(handler):
    a = SellerAPI.__new__(SellerAPI)
    a.name, a.max_retries, a.timeout = "ТЕСТ", 4, 60
    a.calls = []

    def _post(path, payload):
        a.calls.append((path, payload))
        return handler(path, payload)

    a._post = _post
    return a


def item(offer, present, reserved=0):
    return {"offer_id": offer, "product_id": 1,
            "stocks": [{"present": present, "reserved": reserved}]}


print("\n1. Новый формат: items и cursor на верхнем уровне, без result")


def new_shape(path, payload):
    assert "cursor" in payload, payload
    if payload["cursor"] == "":
        return {"items": [item("A", 5), item("B", 0)], "cursor": "c1", "total": 3}
    return {"items": [item("C", 7)], "cursor": "", "total": 3}


a = api_with(new_shape)
st = a.stocks(limit=2)
check("собраны все три артикула", set(st) == {"A", "B", "C"}, st)
check("остаток прочитан", st["A"]["present"] == 5 and st["C"]["present"] == 7, st)
check("постраничность отработала", len(a.calls) == 2, len(a.calls))

print("\n2. Старый формат: result + last_id, cursor отвергается")


def old_shape(path, payload):
    if "cursor" in payload:
        raise SellerAPIError("Request validation error: unknown field cursor", status=400)
    return {"result": {"items": [item("A", 3)], "last_id": ""}}


a = api_with(old_shape)
st = a.stocks(limit=100)
check("откатились на last_id и собрали остатки", st.get("A", {}).get("present") == 3, st)

print("\n3. Пустой ответ больше не выглядит как «всё распродано»")
a = api_with(lambda p, pl: {"items": [], "cursor": ""})
check("вернулся пустой словарь, без исключения", a.stocks() == {})

# предохранитель в фильтре товаров
prods = {"X": {"offer_id": "X", "ordered_units": 5}}
check("при пустых остатках фильтр «на остатках» не режет",
      list(P.filter_products(prods, {}, "OUT", True)) == ["X"])
check("при нормальных остатках режет как раньше",
      list(P.filter_products({"X": {"offer_id": "X"}},
                             {"X": {"present": 0}}, "OUT", True)) == [])

print("\n4. Устаревшая метрика не уносит с собой весь запрос")
S.DEPRECATED_METRICS = set()
DEPRECATED = {"position_category"}


def analytics(path, payload):
    if path != "/v1/analytics/data":
        raise SellerAPIError("не тот адрес")
    used = set(payload["metrics"])
    if used & DEPRECATED:
        raise SellerAPIError('HTTP 400: {"code":3, "message":"deprecated metrics used"}',
                             status=400)
    return {"result": {"data": [
        {"dimensions": [{"id": "111"}], "metrics": [100] * len(payload["metrics"])}
    ]}}


a = api_with(analytics)
rows, order = a.analytics_data("2026-08-01", "2026-08-05", dimension=("sku",),
                               metrics=["revenue", "ordered_units", "position_category"])
check("данные всё-таки получены", len(rows) == 1, rows)
check("устаревшая метрика найдена и запомнена",
      "position_category" in S.DEPRECATED_METRICS, S.DEPRECATED_METRICS)
check("она осталась в отчёте нулём, строка макета не пропала",
      rows[0].get("position_category") == 0 and "position_category" in order,
      rows[0])
check("живые метрики на месте", rows[0]["revenue"] == 100, rows[0])

# второй вызов уже не тратит запросы на поиск виновных
a2 = api_with(analytics)
a2.analytics_data("2026-08-01", "2026-08-05", dimension=("sku",),
                  metrics=["revenue", "position_category"])
check("повторный вызов не ищет заново",
      len(a2.calls) == 1, len(a2.calls))

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
