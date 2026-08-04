#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Точка входа. Формирует отчёты OZON по всем магазинам и заливает их
в Google Таблицы (если включено в config.py).

Примеры:
    python run.py all            # ежедневные отчёты 1-4 — запуск в 8:00
    python run.py cumulative     # 1. накопительная сводка продаж (шт)
    python run.py dod            # 2. динамика день ко дню
    python run.py quality        # 3. качественные показатели по товарам
    python run.py stocks         # 4. остатки по позициям
    python run.py intraday       # 5. промежуточный (каждые 2 часа)

    python run.py all --no-upload            # только файлы, без Google
    python run.py quality --store "ШТУЧКА"   # ограничить магазины
"""

import sys
import argparse
import logging

try:
    import config
except ImportError:
    print("Не найден config.py. Скопируйте config.example.py -> config.py и заполните его.")
    sys.exit(1)

from ozon.collector import StoreCollector
from ozon import reports


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_collectors(store_filter=None):
    cols = []
    for st in config.STORES:
        if store_filter and st["name"] not in store_filter:
            continue
        cols.append(StoreCollector(
            st,
            enable_performance=getattr(config, "ENABLE_PERFORMANCE", True),
            exclude_marker=getattr(config, "EXCLUDE_ARTICLE_MARKER", "OUT"),
        ))
    return cols


COMMANDS = {
    "cumulative": reports.build_cumulative_sales,
    "dod": reports.build_day_over_day,
    "quality": reports.build_quality,
    "stocks": reports.build_stocks,
    "intraday": reports.build_intraday,
}

DAILY = ("cumulative", "dod", "quality", "stocks")

TITLES = {
    "cumulative": "OZON 01. Общая сводная по продажам",
    "dod": "OZON 02. Динамика день ко дню",
    "quality": "OZON 03. Качественные показатели по товарам",
    "stocks": "OZON 04. Остатки по позициям",
    "intraday": "OZON 05. Промежуточный отчёт",
}


def upload_all(made):
    """
    made: список (ключ_отчёта, путь_к_файлу).
    Возвращает список (название, ссылка) для вывода.
    """
    if not getattr(config, "UPLOAD_TO_GOOGLE", False):
        return []

    from ozon.gsheets import GSheetsUploader, GSheetsError
    try:
        up = GSheetsUploader(config.GOOGLE_CREDENTIALS_FILE)
    except GSheetsError as e:
        logging.error("Выгрузка в Google не выполнена: %s", e)
        return []

    sheets = dict(getattr(config, "GOOGLE_SHEETS", {}))
    share = getattr(config, "GOOGLE_SHARE_WITH", [])
    links, new_ids = [], {}

    for key, path in made:
        try:
            sid, url = up.upload(
                path,
                spreadsheet_id=sheets.get(key) or None,
                title=TITLES.get(key, key),
                share_with=share,
            )
            links.append((TITLES.get(key, key), url))
            if not sheets.get(key):
                new_ids[key] = sid
        except GSheetsError as e:
            logging.error("Отчёт «%s» не залит в Google: %s", key, e)

    if new_ids:
        print("\n" + "!" * 64)
        print("Созданы новые таблицы. Впишите эти ID в config.py -> GOOGLE_SHEETS,")
        print("чтобы дальше обновлялись они же и ссылки не менялись:")
        for key, sid in new_ids.items():
            print(f'    "{key}": "{sid}",')
        print("!" * 64)

    return links


def main():
    ap = argparse.ArgumentParser(description="Отчёты OZON по 5 магазинам")
    ap.add_argument("command", choices=list(COMMANDS) + ["all"],
                    help="какой отчёт формировать")
    ap.add_argument("--store", action="append", default=None,
                    help="ограничить магазины (можно указывать несколько раз)")
    ap.add_argument("--no-upload", action="store_true",
                    help="не заливать в Google Таблицы")
    args = ap.parse_args()

    setup_logging()
    cols = build_collectors(args.store)
    if not cols:
        print("Нет магазинов для обработки (проверьте config.py и --store).")
        sys.exit(1)

    keys = DAILY if args.command == "all" else (args.command,)
    made, failed = [], []
    for key in keys:
        try:
            made.append((key, COMMANDS[key](cols, config)))
        except Exception as e:
            logging.error("Отчёт «%s» не сформирован: %s", key, e)
            failed.append(key)

    print("\nГотовые файлы:")
    for _, p in made:
        print("  ", p)

    if not args.no_upload:
        links = upload_all(made)
        if links:
            print("\nGoogle Таблицы:")
            for title, url in links:
                print(f"   {title}\n     {url}")

    if failed:
        print(f"\nНе сформированы: {', '.join(failed)} — смотрите ошибки выше.")
        sys.exit(1)


if __name__ == "__main__":
    main()
