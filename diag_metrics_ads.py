# -*- coding: utf-8 -*-
"""
Две оставшиеся дыры: нулевые метрики аналитики и нулевая «реклама».

1. Из семи метрик отчётов заполняются только «оборот» и «купили». OZON
   молча возвращает МЕНЬШЕ значений, чем запрошено (на семь метрик пришло
   два числа), поэтому показы, клики, корзина, отмены и место в поиске
   оказываются нулями. Скрипт проверяет каждую метрику по отдельности:
   какие живы, какие отвергаются, какие молчат.

2. Расход рекламы не доезжает до отчётов. Скрипт скачивает один настоящий
   отчёт Performance API и печатает заголовки CSV и первые строки — по ним
   видно, как называется колонка с товаром и совпадают ли её значения
   с справочником sku.

    python diag_metrics_ads.py
    python diag_metrics_ads.py --store "ШТУЧКА"
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

from ozon.seller_api import SellerAPI, SellerAPIError, ANALYTICS_METRICS
from ozon.performance_api import PerformanceAPI
from ozon import dates as D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    args = ap.parse_args()

    store = next((s for s in config.STORES
                  if args.store is None or s["name"] == args.store), None)
    if not store:
        print("Магазин не найден")
        sys.exit(1)

    tz = config.TIMEZONE
    d_to = D.yesterday(tz)
    d_from = d_to - timedelta(days=6)
    print(f"Магазин: {store['name']}   период {D.d(d_from)} .. {D.d(d_to)}")

    api = SellerAPI(store["client_id"], store["api_key"], name=store["name"])

    # ---------- 1. Какие метрики ещё живы ----------
    print("\n=== МЕТРИКИ АНАЛИТИКИ (по одной) ===")
    alive, dead, silent = [], [], []
    for m in ANALYTICS_METRICS:
        try:
            data = api._post("/v1/analytics/data", {
                "date_from": D.d(d_from), "date_to": D.d(d_to),
                "metrics": [m], "dimension": ["sku"], "filters": [],
                "sort": [{"key": m, "order": "DESC"}], "limit": 3, "offset": 0,
            })
        except SellerAPIError as e:
            txt = str(e)
            mark = "устарела" if "deprecated" in txt.lower() else "ошибка"
            (dead if mark == "устарела" else silent).append(m)
            print(f"  {m:22} ОТКАЗ ({mark}): {txt[:90]}")
            continue
        res = data.get("result") or {}
        rows = res.get("data") or []
        totals = res.get("totals")
        vals = rows[0].get("metrics") if rows else None
        if vals and any(v for v in vals):
            alive.append(m)
            print(f"  {m:22} ЖИВА   пример {vals}, итог {totals}")
        else:
            silent.append(m)
            print(f"  {m:22} НОЛЬ   строк {len(rows)}, итог {totals}")

    print(f"\n  живых: {len(alive)} -> {alive}")
    print(f"  устаревших: {len(dead)} -> {dead}")
    print(f"  молчащих (0): {len(silent)} -> {silent}")

    # сколько значений вернётся, если попросить все живые разом
    if alive:
        data = api._post("/v1/analytics/data", {
            "date_from": D.d(d_from), "date_to": D.d(d_to),
            "metrics": alive, "dimension": ["sku"], "filters": [],
            "sort": [{"key": alive[0], "order": "DESC"}], "limit": 1, "offset": 0,
        })
        rows = ((data.get("result") or {}).get("data") or [])
        got = len(rows[0].get("metrics", [])) if rows else 0
        print(f"\n  запросили {len(alive)} живых метрик разом -> вернулось {got} значений")
        if got != len(alive):
            print("  >> OZON отдаёт не все: значения сдвигаются и попадают не в свои колонки")

    # ---------- 2. Что в CSV рекламы ----------
    print("\n=== РЕКЛАМА: настоящий CSV ===")
    if not store.get("perf_client_id"):
        print("  рекламные ключи не заданы")
        return
    perf = PerformanceAPI(store["perf_client_id"], store["perf_client_secret"],
                          name=store["name"])
    camps = perf.campaigns(D.d(d_from), D.d(d_to))
    print(f"  кампаний к запросу: {len(camps)}")
    if not camps:
        print("  нечего запрашивать")
        return
    rows = perf._statistics_batch(D.d(d_from), D.d(d_to), camps[:10], "DATE", "проба")
    print(f"  строк в отчёте: {len(rows)}")
    if not rows:
        print("  отчёт пуст")
        return
    print("  КОЛОНКИ:", list(rows[0].keys()))
    for r in rows[:3]:
        print("   ", json.dumps(r, ensure_ascii=False)[:300])

    # как их видит наш разбор
    spend = perf.spend_by_product_day(D.d(d_from), D.d(d_to))
    print(f"\n  наш разбор дал товаров: {len(spend)}")
    for k, v in list(spend.items())[:5]:
        print(f"     ключ {k!r} -> {list(v.items())[:2]}")

    # совпадают ли ключи со справочником
    items = api.product_list()
    sku_map, _ = api.product_info_list([it["product_id"] for it in items
                                        if it.get("product_id")])
    keys = set(spend)
    map_keys = {str(k) for k in sku_map}
    offers = {v.get("offer_id") for v in sku_map.values()}
    print(f"\n  ключей рекламы: {len(keys)}")
    print(f"  совпало с sku справочника:     {len(keys & map_keys)}")
    print(f"  совпало с артикулами:          {len(keys & offers)}")
    if keys and not (keys & map_keys) and not (keys & offers):
        print("  >> ключи не совпадают ни с sku, ни с артикулами —")
        print("     поэтому «реклама» и «ДРР» остаются нулями")
        print("     примеры ключей рекламы:", list(keys)[:5])
        print("     примеры sku справочника:", list(map_keys)[:5])


if __name__ == "__main__":
    main()
