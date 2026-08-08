# -*- coding: utf-8 -*-
"""
product-queries: отказ зависит не от периода, а от частоты. Проверяем.

Вторая проба дала противоречие, которое важнее всей остальной таблицы: окно
29.07 -> 05.08 в ПЕРВОЙ пробе вернуло десять товаров с живыми числами, а во
ВТОРОЙ, спустя минуты, ответило «There is no data for the specified period».
Один и тот же запрос, два разных ответа. Значит текст ошибки врёт: период
тут ни при чём, OZON прикрывает им что-то другое.

Косвенное подтверждение — расположение удач. В первой пробе (10 запросов)
данные пришли на шестом. Во второй (37 запросов подряд, интервал 0.6 с) —
ровно на одиннадцатом, и после него не прошло НИ ОДНОГО, включая то самое
окно 29.07 -> 05.08, которое работало пять минут назад. Похоже на лимит
частоты, замаскированный под ошибку данных: OZON отвечает 200 и пустотой
вместо честного 429.

Скрипт бьёт по ОДНОМУ И ТОМУ ЖЕ заведомо рабочему окну с разными паузами и
печатает сырой ответ вместе с заголовками — в них может оказаться подсказка
про лимит. Если дело в частоте, картина будет такая: первый запрос после
паузы проходит, следующие подряд — нет.

Идёт около 12 минут, почти всё время — ожидание. Рекламу не трогает.

    python diag_queries3.py
    python diag_queries3.py --store "ШТУЧКА" --day 2026-08-04
"""

import sys
import json
import time
import argparse
from datetime import timedelta, date

try:
    import config
except ImportError:
    print("Не найден config.py — запускайте из корня проекта")
    sys.exit(1)

from ozon.seller_api import SellerAPI, BASE_URL
from ozon import dates as D

PATH = "/v1/analytics/product-queries"
HINT_HEADERS = ("x-ratelimit", "ratelimit", "retry-after", "x-request-id",
                "x-quota", "x-limit")

stats = {"ok_data": 0, "ok_empty": 0, "refused": 0, "other": 0}


def call(api, skus, d_from, d_to, label, show_headers=False):
    """Один сырой запрос без ретраев: ретраи смазали бы картину лимита."""
    payload = {
        "date_from": f"{d_from.isoformat()}T00:00:00Z",
        "date_to": f"{d_to.isoformat()}T00:00:00Z",
        "skus": skus,
        "page": 0,
        "page_size": 1000,
    }
    t0 = time.time()
    r = api.session.post(BASE_URL + PATH, json=payload, timeout=60)
    dt = time.time() - t0

    if show_headers:
        hits = {k: v for k, v in r.headers.items()
                if any(h in k.lower() for h in HINT_HEADERS)}
        print(f"      заголовки-подсказки: {hits if hits else 'нет'}")

    if r.status_code != 200:
        body = r.text[:200].replace("\n", " ")
        verdict = ("нет данных за период" if "no data for the specified" in body
                   else f"HTTP {r.status_code}: {body}")
        stats["refused" if "no data for the specified" in body else "other"] += 1
        print(f"   {label:<26} {dt:>5.1f}s  ОТКАЗ: {verdict}")
        return False

    data = r.json()
    items = data.get("items") or []
    views = sum(int(str(i.get("unique_view_users") or 0) or 0) for i in items)
    if items:
        stats["ok_data"] += 1
        print(f"   {label:<26} {dt:>5.1f}s  ДАННЫЕ: товаров {len(items)}, "
              f"показов {views}")
    else:
        stats["ok_empty"] += 1
        print(f"   {label:<26} {dt:>5.1f}s  пусто (200, но товаров 0)")
    return bool(items)


def wait(sec):
    print(f"   ... пауза {sec} с")
    time.sleep(sec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    ap.add_argument("--day", default=None,
                    help="день окна YYYY-MM-DD; по умолчанию сегодня-4")
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

    if args.day:
        y, m, dd = map(int, args.day.split("-"))
        d0 = date(y, m, dd)
    else:
        d0 = D.today() - timedelta(days=4)
    d1 = d0 + timedelta(days=1)
    print(f"рабочее окно: {d0.strftime('%d.%m')} -> {d1.strftime('%d.%m')} "
          f"(во второй пробе оно дало 9 товаров и 17051 показ)")
    print(f"sku: {len(skus)} шт\n")

    print("A. Пять запросов подряд без пауз")
    print("   (если лимит есть — пройдёт только первый)")
    for i in range(1, 6):
        call(api, skus, d0, d1, f"A{i}: подряд", show_headers=(i == 1))

    print("\nB. Пауза 90 с, затем один запрос")
    print("   (проверяем, «отпускает» ли после простоя)")
    wait(90)
    call(api, skus, d0, d1, "B: после 90 с", show_headers=True)

    print("\nC. Ищем минимальную паузу, при которой запрос проходит")
    for pause in (10, 20, 30, 45, 60):
        wait(pause)
        call(api, skus, d0, d1, f"C: пауза {pause} с")

    print("\nD. Пауза 90 с, затем другое окно — то, что работало в пробе 1")
    wait(90)
    w_from = D.today() - timedelta(days=10)
    w_to = D.today() - timedelta(days=3)
    call(api, skus, w_from, w_to,
         f"D: {w_from.strftime('%d.%m')}->{w_to.strftime('%d.%m')}")

    print("\nE. Сразу следом — соседние сутки, без паузы")
    print("   (если после удачи соседний день сразу отказывает — это лимит,")
    print("    а не отсутствие данных за конкретную дату)")
    for back in (5, 6, 7):
        d = D.today() - timedelta(days=back)
        call(api, skus, d, d + timedelta(days=1),
             f"E: {d.strftime('%d.%m')} сутки")

    print("\nF. Пауза 90 с, затем те же соседние сутки по одной")
    for back in (5, 6, 7):
        wait(90)
        d = D.today() - timedelta(days=back)
        call(api, skus, d, d + timedelta(days=1),
             f"F: {d.strftime('%d.%m')} сутки")

    print("\n--- ИТОГ")
    print(f"   с данными: {stats['ok_data']}, пусто: {stats['ok_empty']}, "
          f"«нет данных за период»: {stats['refused']}, прочее: {stats['other']}")
    print("   Если удачи стоят строго после пауз — это лимит частоты,")
    print("   и «показы» по дням собрать можно, просто медленно.")
    print("   Если удач мало и вразнобой — метод отдаёт данные через раз,")
    print("   и на него нельзя опираться в ежедневном отчёте.")
    print("\nГотово. Присылайте вывод целиком.")


if __name__ == "__main__":
    main()
