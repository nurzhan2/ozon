#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разбор рекламных кампаний по магазинам — без сбора статистики.

Стоит один запрос на магазин (плюс токен) и отвечает на вопросы, из-за которых
упирается суточный лимит:

  * сколько кампаний в кабинете и в каких они статусах;
  * сколько из них реально пойдёт в отчёт после фильтров;
  * во сколько запросов обойдётся один проход и хватит ли суточных 2000;
  * какие кампании уже занесены в чёрный список и чем они отличаются
    от рабочих (тип, статус, даты) — это и есть ответ на 400
    «generation of this type of report is forbidden».

Запуск:
    python perf_audit.py
    python perf_audit.py --store "БЬЮТИФУЛ"
"""

import sys
import argparse
from collections import Counter

try:
    import config
except ImportError:
    print("Не найден config.py")
    sys.exit(1)

from ozon.performance_api import (
    PerformanceAPI, MAX_CAMPAIGNS_PER_REQUEST, SKIP_STATES,
    DAILY_BUDGET, OZON_DAILY_LIMIT,
)
from ozon import dates as D


FIELDS = ("id", "title", "state", "advObjectType", "paymentType",
          "fromDate", "toDate", "createdAt", "updatedAt")


def describe(c):
    return {k: c.get(k) for k in FIELDS if c.get(k) not in (None, "")}


def audit(store, date_from, date_to):
    name = store["name"]
    print("\n" + "=" * 72)
    print(name)
    print("=" * 72)
    if not store.get("perf_client_id"):
        print("  рекламные ключи не заданы — пропуск")
        return

    api = PerformanceAPI(store["perf_client_id"], store["perf_client_secret"], name=name)
    raw = api._get("/api/client/campaign").json()
    items = raw.get("list") or raw.get("campaigns") or []
    print(f"  всего кампаний в кабинете: {len(items)}")

    states = Counter(api._state_of(c) for c in items)
    print("  по статусам:   " + ", ".join(f"{k}={v}" for k, v in states.most_common()))

    types = Counter(str(c.get("advObjectType") or "?") for c in items)
    print("  по типам:      " + ", ".join(f"{k}={v}" for k, v in types.most_common()))

    api._campaigns_cache = items
    kept = api.campaigns(date_from, date_to)
    print(f"  пойдёт в отчёт: {len(kept)}")

    batches = (len(kept) + MAX_CAMPAIGNS_PER_REQUEST - 1) // MAX_CAMPAIGNS_PER_REQUEST
    # 1 POST + 1 скачивание + опросы статуса; при нарастающей паузе
    # типичный отчёт укладывается в 3-7 опросов.
    low, high = batches * 5, batches * 9
    print(f"  пачек за проход: {batches} -> примерно {low}-{high} запросов")
    print(f"  проходов в сутки: 1 утренний + 8 промежуточных = 9")
    print(f"  ИТОГО за сутки:  примерно {low * 9}-{high * 9} запросов "
          f"при потолке {DAILY_BUDGET} (лимит OZON {OZON_DAILY_LIMIT})")
    if high * 9 > (DAILY_BUDGET or OZON_DAILY_LIMIT):
        print("  !! не укладывается — сократите INTRADAY_HOURS "
              "или поднимите PERF_DAILY_BUDGET")

    spent = api._usage.get("count", 0)
    print(f"  израсходовано сегодня: {spent}"
          + ("  (лимит уже помечен как исчерпанный)" if api._usage.get("blocked") else ""))

    if api._forbidden:
        print(f"\n  чёрный список ({len(api._forbidden)} шт.) — OZON запретил по ним отчёт:")
        bad = [c for c in items if str(c.get("id")) in api._forbidden]
        for c in bad[:10]:
            print("    " + str(describe(c)))
        if len(bad) > 10:
            print(f"    ... и ещё {len(bad) - 10}")
        good = [c for c in items if str(c.get("id")) in set(kept)]
        if good:
            print("\n  для сравнения — рабочая кампания:")
            print("    " + str(describe(good[0])))
        bad_types = Counter(str(c.get("advObjectType") or "?") for c in bad)
        good_types = Counter(str(c.get("advObjectType") or "?") for c in good)
        print(f"\n  типы запрещённых: {dict(bad_types)}")
        print(f"  типы рабочих:     {dict(good_types)}")
        only_bad = set(bad_types) - set(good_types)
        if only_bad:
            print(f"  >> эти типы встречаются ТОЛЬКО у запрещённых: {sorted(only_bad)}")
            print("     их можно отсеивать сразу, не тратя запросы на поиск")
    else:
        print("  чёрный список пуст")

    print(f"\n  фильтр по статусам сейчас отсекает: {sorted(SKIP_STATES) or '—'}")


def main():
    ap = argparse.ArgumentParser(description="Разбор рекламных кампаний OZON")
    ap.add_argument("--store", action="append", default=None)
    args = ap.parse_args()

    tz = config.TIMEZONE
    date_to = D.d(D.yesterday(tz))
    date_from = D.d(D.month_start(tz_name=tz))
    print(f"Период для оценки: {date_from} .. {date_to}")

    for store in config.STORES:
        if args.store and store["name"] not in args.store:
            continue
        try:
            audit(store, date_from, date_to)
        except Exception as e:
            print(f"  ОШИБКА по магазину {store['name']}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
