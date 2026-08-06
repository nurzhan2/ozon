# -*- coding: utf-8 -*-
"""
Почему пустые отчёты 1-3: точечная проверка /v1/analytics/data.

Остатки и реклама приходят, а продажи по товарам — нет. Значит вопрос к
одному методу. Скрипт дёргает его несколькими способами и печатает СЫРОЙ
ответ OZON, чтобы стало видно: пустые данные, отказ по подписке или
неподдерживаемая метрика.

Стоит около десятка запросов на один магазин.

    python diag_analytics.py
    python diag_analytics.py --store "ШТУЧКА"
"""

import sys
import json
import argparse
from datetime import timedelta

try:
    import config
except ImportError:
    print("Не найден config.py")
    sys.exit(1)

from ozon.seller_api import SellerAPI, SellerAPIError
from ozon import dates as D


def show(api, title, payload):
    print(f"\n--- {title}")
    print("   запрос:", json.dumps(payload, ensure_ascii=False)[:200])
    try:
        data = api._post("/v1/analytics/data", payload)
    except SellerAPIError as e:
        print("   ОТКАЗ:", str(e)[:400])
        return None
    res = data.get("result") or {}
    rows = res.get("data") or []
    print(f"   строк: {len(rows)}; totals: {res.get('totals')}")
    if rows:
        print("   первая строка:", json.dumps(rows[0], ensure_ascii=False)[:300])
    else:
        # печатаем ответ целиком: там может быть подсказка про доступ
        print("   ответ целиком:", json.dumps(data, ensure_ascii=False)[:600])
    return rows


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
        print("Магазин не найден")
        sys.exit(1)

    tz = config.TIMEZONE
    d_to = D.yesterday(tz)
    d_from = d_to - timedelta(days=6)
    print(f"Магазин: {store['name']}, client_id={store['client_id']}")
    print(f"Период: {D.d(d_from)} .. {D.d(d_to)}")

    api = SellerAPI(store["client_id"], store["api_key"], name=store["name"])

    base = {"date_from": D.d(d_from), "date_to": D.d(d_to),
            "filters": [], "limit": 100, "offset": 0}

    # 1. Ровно то, что просят отчёты 1-3
    show(api, "как в отчётах: sku+day, 7 метрик", dict(
        base, dimension=["sku", "day"],
        metrics=["revenue", "ordered_units", "cancellations", "hits_view",
                 "session_view", "hits_tocart", "position_category"],
        sort=[{"key": "revenue", "order": "DESC"}]))

    # 2. То же без разбивки по дням — этим пользуется отчёт 4
    show(api, "как в отчёте 4: только sku", dict(
        base, dimension=["sku"],
        metrics=["revenue", "ordered_units"],
        sort=[{"key": "revenue", "order": "DESC"}]))

    # 3. Минимум: одна метрика, одно измерение
    show(api, "минимум: sku + ordered_units", dict(
        base, dimension=["sku"], metrics=["ordered_units"],
        sort=[{"key": "ordered_units", "order": "DESC"}]))

    # 4. День без товара — покажет, есть ли вообще продажи в периоде
    show(api, "без товара: только day", dict(
        base, dimension=["day"], metrics=["revenue", "ordered_units"],
        sort=[{"key": "revenue", "order": "DESC"}]))

    # 5. Подозрительная метрика отдельно
    show(api, "отдельно position_category (может не поддерживаться)", dict(
        base, dimension=["sku"], metrics=["position_category"],
        sort=[{"key": "position_category", "order": "DESC"}]))

    # 6. Период пошире — вдруг дело в датах
    show(api, "период 30 дней, только sku", dict(
        base, date_from=D.d(d_to - timedelta(days=29)),
        dimension=["sku"], metrics=["revenue", "ordered_units"],
        sort=[{"key": "revenue", "order": "DESC"}]))

    print("\nЧто смотреть:")
    print("  * везде 0 строк -> метод не отдаёт данные этому аккаунту")
    print("    (у OZON аналитика доступна не на всех тарифах);")
    print("  * работает только без 'day' -> дело в разбивке по дням;")
    print("  * падает только position_category -> уберём эту метрику.")


if __name__ == "__main__":
    main()
