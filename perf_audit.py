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
    DAILY_BUDGET, OZON_DAILY_LIMIT, STALE_DAYS, _norm_date,
)
from ozon import dates as D


FIELDS = ("id", "title", "state", "advObjectType", "paymentType",
          "fromDate", "toDate", "createdAt", "updatedAt")


def describe(c):
    return {k: c.get(k) for k in FIELDS if c.get(k) not in (None, "")}


def _norm(x):
    return _norm_date(x)


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

    # --- сколько сэкономил бы отсев неработающих и давно не менявшихся ---
    print("\n  отсев по PERF_STALE_DAYS (сейчас "
          f"{STALE_DAYS if STALE_DAYS else 'выключен'}):")
    kept_set = set(kept)
    candidates = [c for c in items if str(c.get("id")) in kept_set]
    for days in (0, 3, 7, 30):
        stale = [c for c in candidates
                 if PerformanceAPI.is_stale(c, date_from, days)] if days else []
        left = len(candidates) - len(stale)
        b = (left + MAX_CAMPAIGNS_PER_REQUEST - 1) // MAX_CAMPAIGNS_PER_REQUEST
        mark = "  <- сейчас" if days == STALE_DAYS else ""
        print(f"    {days:>3} дн: отсеет {len(stale):>3}, останется {left:>3} "
              f"-> {b} пачек, примерно {b * 5 * 9}-{b * 9 * 9} запросов в сутки{mark}")
    stale7 = [c for c in candidates if PerformanceAPI.is_stale(c, date_from, 7)]
    if stale7:
        newest = max(PerformanceAPI.last_touch(c) for c in stale7)
        print(f"    самая свежая из отсеиваемых при 7 дн менялась {newest} "
              f"(период начинается {date_from})")

    if api._forbidden:
        bad = [c for c in items if str(c.get("id")) in api._forbidden]
        print(f"\n  чёрный список ({len(api._forbidden)} шт.) — OZON запретил по ним отчёт:")
        for c in bad[:10]:
            print("    " + str(describe(c)))
        if len(bad) > 10:
            print(f"    ... и ещё {len(bad) - 10}")

        bad_types = Counter(str(c.get("advObjectType") or "?") for c in bad)
        cand_types = Counter(str(c.get("advObjectType") or "?") for c in candidates)
        print(f"\n  типы запрещённых:              {dict(bad_types)}")
        print(f"  типы кандидатов в отчёт:       {dict(cand_types)}")
        print("  (кандидаты — это те, кого мы СОБИРАЕМСЯ запросить, а не те,")
        print("   про кого известно, что отчёт по ним проходит)")

        # Тип сам по себе редко виноват: у одного магазина SEARCH_PROMO
        # отдаётся, у другого нет. Разделяет обычно возраст кампании.
        bad_created = sorted(_norm(c.get("createdAt")) for c in bad
                             if _norm(c.get("createdAt")))
        if bad_created:
            print(f"  запрещённые созданы: {bad_created[0]} .. {bad_created[-1]}")
        for t in sorted(set(bad_types)):
            same = [c for c in candidates
                    if str(c.get("advObjectType") or "?") == t]
            created = sorted(_norm(c.get("createdAt")) for c in same
                             if _norm(c.get("createdAt")))
            if created:
                print(f"  кандидаты типа {t}: {len(same)} шт., "
                      f"созданы {created[0]} .. {created[-1]}")
        only_bad = set(bad_types) - set(cand_types)
        if only_bad:
            print(f"  >> тип {sorted(only_bad)} остался только у запрещённых — "
                  "можно отсекать сразу")
    else:
        print("\n  чёрный список пуст")

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
