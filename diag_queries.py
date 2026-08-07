# -*- coding: utf-8 -*-
"""
Почему /v1/analytics/product-queries отвечает 400 getPremiumAnalyticsPeriod.

В боевом логе текст ошибки обрезан на 200 символах и заканчивается на
«desc = There is n...» — по нему нельзя понять, дело в подписке или в датах.
Документация говорит, что метод работает и на обычном Premium, и даже без
подписки («без подписки вы можете посмотреть часть показателей»), но данные
за последний месяц доступны «в любом интервале, кроме текущей даты», а
раньше месяца — только по неделям.

Скрипт дёргает метод десятком разных способов и печатает ПОЛНЫЙ ответ OZON,
чтобы стало видно, какая форма запроса проходит.

    python diag_queries.py
    python diag_queries.py --store "ШТУЧКА"
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


def show(api, title, payload):
    print(f"\n--- {title}")
    p = dict(payload)
    if "skus" in p:
        p["skus"] = f"<{len(p['skus'])} шт: {', '.join(p['skus'][:3])}...>"
    print("   запрос:", json.dumps(p, ensure_ascii=False))
    try:
        data = api._post("/v1/analytics/product-queries", payload)
    except SellerAPIError as e:
        # ПОЛНЫЙ текст, без обрезки — ради него всё и затевалось
        print("   ОТКАЗ:", str(e))
        return None
    items = data.get("items") or []
    print(f"   OK: товаров {len(items)}, страниц {data.get('page_count')}, "
          f"всего {data.get('total')}")
    print("   период в ответе:",
          json.dumps(data.get("analytics_period"), ensure_ascii=False))
    if items:
        print("   первый товар:", json.dumps(items[0], ensure_ascii=False)[:500])
    return data


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

    # берём немного реальных sku
    skus = []
    for p in api.product_list():
        sku = p.get("sku") or p.get("fbo_sku") or p.get("fbs_sku")
        if sku:
            skus.append(str(sku))
        if len(skus) >= 10:
            break
    if not skus:
        print("Не удалось получить ни одного sku — дальше смысла нет")
        sys.exit(1)
    print(f"sku для проверки: {len(skus)} шт ({', '.join(skus[:5])}...)")

    today = D.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d3 = (today - timedelta(days=3)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()
    d10 = (today - timedelta(days=10)).isoformat()

    base = {"skus": skus, "page": 0, "page_size": 1000}

    # 1. То, что делает боевой код сейчас: один день, с временем
    show(api, "1. один день, ...T00:00:00Z / ...T23:59:59Z (как в проде)",
         dict(base, date_from=f"{d3}T00:00:00Z", date_to=f"{d3}T23:59:59Z",
              sort_by="BY_SEARCHES", sort_dir="DESCENDING"))

    # 2. Тот же день, но обе границы в полночь
    show(api, "2. один день, обе границы T00:00:00Z",
         dict(base, date_from=f"{d3}T00:00:00Z", date_to=f"{d3}T00:00:00Z"))

    # 3. Один день, голые даты без времени
    show(api, "3. один день, голые даты YYYY-MM-DD",
         dict(base, date_from=d3, date_to=d3))

    # 4. Только date_from — по документации date_to необязателен
    show(api, "4. только date_from (date_to не передаём)",
         dict(base, date_from=f"{d3}T00:00:00Z"))

    # 5. Интервал в несколько дней
    show(api, "5. интервал 7 дней назад ... вчера",
         dict(base, date_from=f"{d7}T00:00:00Z", date_to=f"{d1}T00:00:00Z"))

    # 6. Интервал ровно в неделю (документация про «только по неделям»)
    show(api, "6. интервал ровно неделя: -10 ... -3",
         dict(base, date_from=f"{d10}T00:00:00Z", date_to=f"{d3}T00:00:00Z"))

    # 7. Вчера — вдруг свежие даты просто ещё не посчитаны
    show(api, "7. вчерашний день",
         dict(base, date_from=f"{d1}T00:00:00Z", date_to=f"{d1}T23:59:59Z"))

    # 8. Сегодня — должно отказать по документации, нужно для контраста
    show(api, "8. сегодня (по документации недоступно — контрольный)",
         dict(base, date_from=f"{today.isoformat()}T00:00:00Z"))

    # 9. Без сортировки и с одним sku — минимально возможный запрос
    show(api, "9. минимальный запрос: один sku, только date_from",
         {"skus": skus[:1], "page": 0, "page_size": 1000,
          "date_from": f"{d3}T00:00:00Z"})

    # 10. Соседний метод: детализация по одному товару
    print("\n--- 10. соседний метод /v1/analytics/product-queries/details")
    try:
        data = api._post("/v1/analytics/product-queries/details", {
            "date_from": f"{d3}T00:00:00Z",
            "skus": skus[:1],
            "page": 0,
            "page_size": 100,
        })
        print("   OK:", json.dumps(data, ensure_ascii=False)[:600])
    except SellerAPIError as e:
        print("   ОТКАЗ:", str(e))

    print("\nГотово. Присылайте вывод целиком.")


if __name__ == "__main__":
    main()
