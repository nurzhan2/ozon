# -*- coding: utf-8 -*-
"""
Какие периоды product-queries вообще принимает.

Первая проба показала, что дело не в подписке: полный текст отказа — «There
is no data for the specified period». Прошло ровно одно окно из десяти:
29.07 00:00 -> 05.08 00:00, то есть ровно семь суток от полуночи до полуночи.
Отказы получили окна с концом в 23:59:59 и окно длиной шесть суток. Окно
нулевой длины (date_from == date_to) отказа не даёт, но возвращает пусто.

Отсюда две догадки, и обе нужно проверить, а не выбирать по вкусу:
  а) OZON принимает только границы по полуночи, и наш 23:59:59 всё ломал —
     тогда обычные сутки 05.08 00:00 -> 06.08 00:00 пройдут и дадут данные;
  б) OZON отдаёт только недельные корзины — тогда сутки вернут пусто или
     отказ, а пройдут только семидневные окна.

Скрипт перебирает окна разной длины по сетке полуночей и печатает, что
вернулось. Заодно пробует /details с обязательным limit_by_sku.

Около сорока запросов на магазин, рекламу не трогает.

    python diag_queries2.py
    python diag_queries2.py --store "ШТУЧКА"
"""

import sys
import json
import argparse
from datetime import timedelta

try:
    import config
except ImportError:
    print("Не найден config.py — запускайте из корня проекта")
    sys.exit(1)

from ozon.seller_api import SellerAPI, SellerAPIError
from ozon import dates as D

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def probe(api, skus, d_from, d_to, note=""):
    """Одно окно. Возвращает (итогов, сумма показов) или None при отказе."""
    label = (f"{d_from.strftime('%d.%m')}({WD[d_from.weekday()]})"
             f" -> {d_to.strftime('%d.%m')}({WD[d_to.weekday()]})"
             f"  {(d_to - d_from).days} сут")
    payload = {
        "date_from": f"{d_from.isoformat()}T00:00:00Z",
        "date_to": f"{d_to.isoformat()}T00:00:00Z",
        "skus": skus,
        "page": 0,
        "page_size": 1000,
    }
    try:
        data = api._post("/v1/analytics/product-queries", payload)
    except SellerAPIError as e:
        txt = str(e)
        short = "нет данных за период" if "no data for the specified" in txt \
            else txt[-90:]
        print(f"   {label:<34} ОТКАЗ: {short}")
        return None
    items = data.get("items") or []
    views = sum(int(str(i.get("unique_view_users") or 0) or 0) for i in items)
    print(f"   {label:<34} товаров {len(items):>3}, показов {views:>8}"
          + (f"   <- {note}" if note else ""))
    return len(items), views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    args = ap.parse_args()

    store = None
    for s in config.STORES:
        if args.store is None or s["name"] == args.store:
            store = s
            break
    if not store:
        print("Магазин не найден:", args.store)
        sys.exit(1)

    api = SellerAPI(store["client_id"], store["api_key"], store["name"])
    print(f"\n================ {store['name']} ================")

    skus = []
    for p in api.product_list():
        sku = p.get("sku") or p.get("fbo_sku") or p.get("fbs_sku")
        if sku:
            skus.append(str(sku))
        if len(skus) >= 10:
            break
    if not skus:
        print("Не удалось получить ни одного sku")
        sys.exit(1)
    print(f"sku: {len(skus)} шт")

    today = D.today()
    print(f"сегодня: {today.strftime('%d.%m.%Y')} ({WD[today.weekday()]})")

    print("\n1. СУТКИ по сетке полуночей: d 00:00 -> d+1 00:00")
    print("   (если хоть одни сутки дадут данные — дневная разбивка возможна)")
    for back in range(14, 1, -1):
        d = today - timedelta(days=back)
        probe(api, skus, d, d + timedelta(days=1))

    print("\n2. ОКНА РАЗНОЙ ДЛИНЫ, конец фиксирован на позавчера")
    end = today - timedelta(days=2)
    for length in (1, 2, 3, 4, 5, 6, 7, 8, 14):
        probe(api, skus, end - timedelta(days=length), end,
              "прошло в первой пробе" if length == 7 else "")

    print("\n3. СЕМИДНЕВНЫЕ ОКНА со сдвигом на день")
    print("   (покажет, привязана ли неделя к конкретному дню недели)")
    for shift in range(0, 8):
        end = today - timedelta(days=2 + shift)
        probe(api, skus, end - timedelta(days=7), end)

    print("\n4. Насколько глубоко лежат данные: недели всё дальше в прошлое")
    for weeks in range(1, 7):
        end = today - timedelta(days=2 + 7 * (weeks - 1))
        probe(api, skus, end - timedelta(days=7), end, f"{weeks}-я неделя назад")

    print("\n5. /v1/analytics/product-queries/details с limit_by_sku")
    end = today - timedelta(days=2)
    payload = {
        "date_from": f"{(end - timedelta(days=7)).isoformat()}T00:00:00Z",
        "date_to": f"{end.isoformat()}T00:00:00Z",
        "skus": skus[:1],
        "limit_by_sku": 10,
        "page": 0,
        "page_size": 100,
    }
    try:
        data = api._post("/v1/analytics/product-queries/details", payload)
        print("   OK:", json.dumps(data, ensure_ascii=False)[:900])
    except SellerAPIError as e:
        print("   ОТКАЗ:", str(e)[:400])

    print("\nГотово. Присылайте вывод целиком.")


if __name__ == "__main__":
    main()
