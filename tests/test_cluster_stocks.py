# -*- coding: utf-8 -*-
"""Кластерные остатки: /v1/analytics/stocks хочет список sku пачками по 100."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon.seller_api import SellerAPI, SellerAPIError

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


def make(handler):
    api = SellerAPI.__new__(SellerAPI)
    api.name = "ТЕСТ"
    api.calls = []

    def _post(path, payload):
        api.calls.append((path, payload))
        return handler(path, payload)

    api._post = _post
    return api


def item(sku, cluster):
    return {"offer_id": f"A{sku}", "sku": sku, "name": "Товар",
            "cluster_name": cluster, "warehouse_name": "СК",
            "available_stock_count": 5, "requested_stock_count": 2,
            "transit_stock_count": 3, "ads": 1.5, "idc": 4.0}


print("\n1. Обычный случай: sku уходят в /v1/analytics/stocks")


def good(path, payload):
    if path == "/v1/analytics/stocks":
        return {"items": [item(s, "Москва") for s in payload["skus"]]}
    raise SellerAPIError("не должно вызываться")


api = make(good)
rows = api.cluster_stocks(skus=[101, 102, 103])
check("строк столько же, сколько sku", len(rows) == 3, len(rows))
check("запрос ровно один", len(api.calls) == 1, api.calls)
check("ушёл именно skus, без limit/offset",
      api.calls[0][1] == {"skus": ["101", "102", "103"]}, api.calls[0][1])
check("«в пути» доехало", rows[0]["transit"] == 3, rows[0])
check("среднесуточные продажи доехали", rows[0]["ads"] == 1.5, rows[0])

print("\n2. Больше сотни sku — режется на пачки")
api = make(good)
rows = api.cluster_stocks(skus=list(range(1, 251)))
check("три запроса на 250 sku", len(api.calls) == 3, len(api.calls))
check("в пачке не больше 100",
      all(len(c[1]["skus"]) <= 100 for c in api.calls),
      [len(c[1]["skus"]) for c in api.calls])
check("собраны все 250 строк", len(rows) == 250, len(rows))

print("\n3. Повторы sku не превращаются в лишние запросы")
api = make(good)
api.cluster_stocks(skus=[7, 7, 7, 8])
check("дубликаты схлопнуты", api.calls[0][1] == {"skus": ["7", "8"]}, api.calls[0][1])

print("\n4. Пустой список — на склады, а не 400 от OZON")


def old_path_only(path, payload):
    if path == "/v1/analytics/stocks":
        raise SellerAPIError("400 Skus: value must contain between 1 and 100 items")
    raise SellerAPIError("404")


api = make(old_path_only)
try:
    api.cluster_stocks(skus=[])
    check("должно было подняться исключение", False)
except SellerAPIError as e:
    check("поднято SellerAPIError — вызывающий уйдёт на склады", True)
    check("пустой список не ушёл в OZON",
          not any(c[0] == "/v1/analytics/stocks" for c in api.calls), api.calls)

print("\n5. Если новый путь отказал — пробуем старый с пагинацией")


def legacy(path, payload):
    if path == "/v1/analytics/stocks":
        raise SellerAPIError("400 что-то не то")
    if path == "/v1/analytics/manage/stocks":
        if payload.get("limit") == 1:
            return {"items": []}
        return {"items": [item(1, "Урал")]}
    raise SellerAPIError("404")


api = make(legacy)
rows = api.cluster_stocks(skus=[1])
check("старый путь отработал", len(rows) == 1, rows)
check("новый путь был опробован первым",
      api.calls[0][0] == "/v1/analytics/stocks", api.calls[0][0])

print("\n6. Оба пути мертвы — ошибка с обеими причинами")


def dead(path, payload):
    raise SellerAPIError(f"нет доступа к {path}")


api = make(dead)
try:
    api.cluster_stocks(skus=[1])
    check("должно было подняться исключение", False)
except SellerAPIError as e:
    check("в тексте видно оба адреса",
          "analytics/stocks" in str(e) and "manage/stocks" in str(e), str(e))

print("\n7. Жёсткий лимит /v1/analytics/stocks: больше попыток и длиннее паузы")
import ozon.seller_api as S


class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data or {}
        self.text = "rate limit per second" if code == 429 else ""

    def json(self):
        return self._data


def api_with(responses):
    a = S.SellerAPI.__new__(S.SellerAPI)
    a.name, a.max_retries, a.timeout = "ТЕСТ", 4, 60
    a.session = type("S", (), {})()
    a.calls = []

    def post(url, json=None, timeout=None):
        a.calls.append(url)
        return responses[min(len(a.calls) - 1, len(responses) - 1)]

    a.session.post = post
    return a


slept = []
real_sleep = S.time.sleep
S.time.sleep = lambda x: slept.append(x)
try:
    # шесть отказов подряд, затем успех: обычному адресу попыток не хватит,
    # «медленному» — хватит
    seq = [Resp(429)] * 6 + [Resp(200, {"items": []})]
    a = api_with(seq)
    try:
        a._post("/v1/product/list", {})
        check("обычный адрес сдаётся на 4 попытках", False)
    except S.SellerAPIError:
        check("обычный адрес сдаётся на 4 попытках", len(a.calls) == 4, len(a.calls))

    slept.clear()
    a = api_with(seq)
    out = a._post("/v1/analytics/stocks", {})
    check("медленный адрес дотерпел до успеха", out == {"items": []}, out)
    check("и потратил 7 попыток", len(a.calls) == 7, len(a.calls))
    # в slept попадают и паузы ограничителя частоты — берём только паузы
    # между попытками (они целые)
    waits = [x for x in slept if isinstance(x, int)]
    check("паузы длиннее прежних 5-20 с",
          waits[:4] == [15, 30, 45, 60], waits[:4])

    # шаг между вызовами выдерживается
    S.time.sleep = lambda x: slept.append(x)
    slept.clear()
    S._LAST_SLOW_CALL.clear()
    S._throttle("/v1/analytics/stocks")
    S._throttle("/v1/analytics/stocks")
    check("между двумя вызовами выдержан шаг",
          any(x >= 3.5 for x in slept), slept)
finally:
    S.time.sleep = real_sleep

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
