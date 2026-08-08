# -*- coding: utf-8 -*-
"""
product-queries: работает ли метод в начале суток, до всех остальных запросов.

Что известно на сейчас. Отказ звучит как «There is no data for the specified
period», но к периоду отношения не имеет: окно 04.08 -> 05.08 в одной пробе
вернуло 9 товаров и 17051 показ, а часом позже то же самое окно отказывало
подряд пять раз, и после паузы в 90 секунд тоже. Значит ни формат дат, ни
длина окна, ни выравнивание по неделям, ни частота запросов картину не
объясняют — все четыре версии проверены и не подтвердились.

Осталась одна непроверенная: суточная квота на «премиальную аналитику».
Она объясняет то, что видели: в первой пробе удачи были, во второй — только
одна, в третьей ни одной. Квота выбирается по ходу дня и не восстанавливается
паузами. Проверяется это ровно одним способом — запросом в начале новых
суток, ДО того как по аккаунту пройдёт что-нибудь ещё.

Поэтому запускать надо утром, первым делом, до run.py. Скрипт короткий:
шесть запросов с шагом в 20 секунд, обрыв связи переживает.

    python diag_queries4.py
    python diag_queries4.py --store "ШТУЧКА"
"""

import sys
import time
import argparse
from datetime import timedelta

try:
    import config
except ImportError:
    print("Не найден config.py — запускайте из корня проекта")
    sys.exit(1)

import requests
from ozon.seller_api import SellerAPI, BASE_URL
from ozon import dates as D

PATH = "/v1/analytics/product-queries"
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def call(api, skus, d_from, d_to):
    """Один запрос. Обрыв связи не роняет скрипт: пробуем ещё раз."""
    payload = {
        "date_from": f"{d_from.isoformat()}T00:00:00Z",
        "date_to": f"{d_to.isoformat()}T00:00:00Z",
        "skus": skus,
        "page": 0,
        "page_size": 1000,
    }
    label = (f"{d_from.strftime('%d.%m')}({WD[d_from.weekday()]})"
             f" -> {d_to.strftime('%d.%m')}")
    for attempt in (1, 2):
        try:
            r = api.session.post(BASE_URL + PATH, json=payload, timeout=60)
            break
        except requests.RequestException as e:
            if attempt == 2:
                print(f"   {label:<22} СЕТЬ: {str(e)[:90]}")
                return None
            print(f"   {label:<22} обрыв связи, повтор через 5 с")
            time.sleep(5)

    if r.status_code != 200:
        body = r.text[:160].replace("\n", " ")
        short = ("нет данных за период" if "no data for the specified" in body
                 else f"HTTP {r.status_code}: {body}")
        print(f"   {label:<22} ОТКАЗ: {short}")
        return False

    items = r.json().get("items") or []
    views = sum(int(str(i.get("unique_view_users") or 0) or 0) for i in items)
    if items:
        print(f"   {label:<22} ДАННЫЕ: товаров {len(items)}, показов {views}")
        return True
    print(f"   {label:<22} пусто (200, товаров 0)")
    return False


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
    today = D.today()
    print(f"\n================ {store['name']} ================")
    print(f"сегодня: {today.strftime('%d.%m.%Y')} ({WD[today.weekday()]})")

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
    print("\nСутки по одним, шаг 20 с. Важен ПЕРВЫЙ результат: если данные")
    print("придут на нём и пропадут дальше — это суточная квота.\n")

    results = []
    for i, back in enumerate((2, 3, 4, 5, 6, 7)):
        if i:
            time.sleep(20)
        d = today - timedelta(days=back)
        results.append(call(api, skus, d, d + timedelta(days=1)))

    good = sum(1 for x in results if x)
    print(f"\n--- ИТОГ: с данными {good} из {len(results)}")
    if results and results[0] and good == 1:
        print("   Данные пришли только на первом запросе — суточная квота,")
        print("   и её хватает на считанные вызовы. Пять магазинов по семь")
        print("   дней в неё не уложатся.")
    elif good == len(results):
        print("   Прошли все — метод жив с утра, вчерашние отказы были")
        print("   следствием того, что квоту выбрали пробы.")
    elif good == 0:
        print("   Не прошёл ни один даже с утра. Дело не в квоте и не в")
        print("   частоте: метод на этом аккаунте просто не отдаёт данные.")
    else:
        print("   Картина смешанная — метод отдаёт данные через раз.")

    print("\nГотово. Присылайте вывод целиком.")


if __name__ == "__main__":
    main()
