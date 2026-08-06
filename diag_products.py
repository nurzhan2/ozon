# -*- coding: utf-8 -*-
"""
Почему отчёты пустые при живой аналитике: сверка справочника товаров и sku.

Аналитика отдаёт продажи по sku (например 4267040923). Отчёты сопоставляют
эти sku со справочником товаров и берут только те, что нашлись и лежат на
остатках. Если справочник sku не покрывает — строка молча выбрасывается,
и отчёт получается пустым при работающем API.

Скрипт печатает обе стороны и их пересечение. Около десяти запросов.

    python diag_products.py
    python diag_products.py --store "ШТУЧКА"
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

from ozon.seller_api import SellerAPI
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

    api = SellerAPI(store["client_id"], store["api_key"], name=store["name"])
    tz = config.TIMEZONE
    d_to = D.yesterday(tz)
    d_from = d_to - timedelta(days=6)
    print(f"Магазин: {store['name']}   период {D.d(d_from)} .. {D.d(d_to)}")

    # --- сторона 1: справочник ---
    items = api.product_list()
    pids = [it["product_id"] for it in items if it.get("product_id")]
    print(f"\n1. /v3/product/list вернул товаров: {len(items)}")
    for it in items[:3]:
        print("   ", json.dumps(it, ensure_ascii=False)[:200])

    raw = api._post("/v3/product/info/list",
                    {"product_id": pids[:100], "offer_id": [], "sku": []})
    ritems = (raw.get("result") or {}).get("items") or raw.get("items") or []
    print(f"\n2. /v3/product/info/list вернул записей: {len(ritems)}")
    if ritems:
        first = ritems[0]
        print("   поля первой записи:", sorted(first.keys()))
        for k in ("id", "product_id", "sku", "fbo_sku", "fbs_sku", "sources",
                  "offer_id", "name", "is_archived", "archived"):
            if k in first:
                v = first[k]
                print(f"     {k} = {json.dumps(v, ensure_ascii=False)[:160]}")

    sku_map, offer_map = api.product_info_list(pids)
    print(f"\n3. Справочник sku -> артикул: {len(sku_map)} записей")
    for k, v in list(sku_map.items())[:5]:
        print(f"     {k} -> {v.get('offer_id')} | {str(v.get('name'))[:60]}")

    # --- сторона 2: аналитика ---
    rows, order = api.analytics_data(D.d(d_from), D.d(d_to),
                                     dimension=("sku",),
                                     metrics=["revenue", "ordered_units"])
    an_sku = {}
    for r in rows:
        dims = r.get("dimensions") or []
        if dims:
            an_sku[str(dims[0].get("id"))] = dims[0].get("name", "")
    print(f"\n4. Аналитика вернула товаров (sku): {len(an_sku)}")
    for k, v in list(an_sku.items())[:5]:
        print(f"     {k} | {str(v)[:60]}")

    # --- пересечение ---
    map_keys = {str(k) for k in sku_map}
    both = map_keys & set(an_sku)
    only_analytics = set(an_sku) - map_keys
    print(f"\n5. ПЕРЕСЕЧЕНИЕ")
    print(f"   sku и там и там:            {len(both)}")
    print(f"   есть в аналитике, нет в справочнике: {len(only_analytics)}")
    print(f"   есть в справочнике, нет в аналитике: {len(map_keys - set(an_sku))}")
    if only_analytics:
        print("   примеры потерянных (именно они и выпадают из отчётов):")
        for k in list(only_analytics)[:8]:
            print(f"     {k} | {str(an_sku[k])[:70]}")

    # --- остатки ---
    stocks = api.stocks()
    with_stock = {o for o, st in stocks.items() if st.get("present", 0) > 0}
    print(f"\n6. Остатки: артикулов всего {len(stocks)}, с остатком > 0: {len(with_stock)}")

    mapped_offers = {v.get("offer_id") for k, v in sku_map.items()
                     if str(k) in both and v.get("offer_id")}
    print(f"   из сопоставленных попадут в отчёт (есть остаток): "
          f"{len(mapped_offers & with_stock)} из {len(mapped_offers)}")

    print("\nВывод:")
    if len(both) == 0:
        print("  Справочник и аналитика не пересекаются вовсе — отчёты будут пустыми.")
    elif only_analytics:
        print(f"  {len(only_analytics)} товаров с продажами не имеют записи в справочнике")
        print("  и молча выбрасываются. Это и есть причина пустых отчётов 1-3.")
    elif not (mapped_offers & with_stock):
        print("  Сопоставление есть, но ни у одного товара нет остатка > 0,")
        print("  а отчёты берут только товары на остатках.")
    else:
        print("  Сопоставление и остатки в порядке — причина в другом месте.")


if __name__ == "__main__":
    main()
