#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка и настройка выгрузки в Google Таблицы.

Что делает:
  1. Читает JSON-ключ сервисного аккаунта и показывает его e-mail
     (именно этому адресу нужно дать доступ к таблицам).
  2. Проверяет каждую таблицу из GOOGLE_SHEETS: существует ли, Google
     Таблица ли это, есть ли права на запись.
  3. Если ID не заданы — по флагу --create создаёт таблицы и печатает ID.

Запуск:
    python setup_gsheets.py            # только проверка
    python setup_gsheets.py --create   # создать недостающие таблицы
"""

import sys
import argparse
import logging

try:
    import config
except ImportError:
    print("Не найден config.py. Задайте переменные окружения или local_config.py")
    sys.exit(1)

from ozon.gsheets import GSheetsUploader, GSheetsError

logging.basicConfig(level=logging.ERROR)

NAMES = {
    "cumulative": "OZON 01. Общая сводная по продажам",
    "dod": "OZON 02. Динамика день ко дню",
    "quality": "OZON 03. Качественные показатели по товарам",
    "stocks": "OZON 04. Остатки по позициям",
    "intraday": "OZON 05. Промежуточный отчёт",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true",
                    help="создать таблицы, для которых не указан ID")
    args = ap.parse_args()

    try:
        up = GSheetsUploader(config.GOOGLE_CREDENTIALS_FILE)
    except GSheetsError as e:
        print("ОШИБКА:", e)
        sys.exit(1)

    print("=" * 66)
    print("Сервисный аккаунт:", up.email)
    print("Именно этому адресу нужно дать доступ РЕДАКТОРА к таблицам.")
    print("=" * 66)

    sheets = dict(getattr(config, "GOOGLE_SHEETS", {}))
    share = getattr(config, "GOOGLE_SHARE_WITH", [])
    problems, created = [], {}

    for key, title in NAMES.items():
        sid = sheets.get(key) or ""
        if sid:
            try:
                name = up.check(sid)
                print(f"  OK    {key:<11} «{name}»")
                print(f"        https://docs.google.com/spreadsheets/d/{sid}")
            except GSheetsError as e:
                print(f"  ОШИБКА {key:<11} {e}")
                problems.append(key)
            continue

        if not args.create:
            print(f"  !     {key:<11} ID не указан "
                  f"(создайте таблицу вручную или запустите с --create)")
            problems.append(key)
            continue

        # создаём пустую таблицу
        try:
            import tempfile, os
            from openpyxl import Workbook
            wb = Workbook()
            wb.active.title = "Лист1"
            tmp = os.path.join(tempfile.gettempdir(), f"{key}.xlsx")
            wb.save(tmp)
            sid, url = up.upload(tmp, spreadsheet_id=None, title=title, share_with=share)
            os.remove(tmp)
            created[key] = sid
            print(f"  СОЗДАНА {key:<10} {url}")
        except GSheetsError as e:
            print(f"  ОШИБКА {key:<11} {e}")
            problems.append(key)

    if created:
        print("\n" + "!" * 66)
        print("Впишите эти ID в переменные Railway:")
        for key, sid in created.items():
            print(f'    GOOGLE_SHEET_{key.upper()} = {sid}')
        print("!" * 66)

    print()
    if problems:
        print(f"Не готово: {', '.join(problems)}")
        print("Исправьте и запустите проверку ещё раз.")
        sys.exit(1)
    print("ВСЁ ГОТОВО — выгрузка в Google Таблицы настроена.")
    sys.exit(0)


if __name__ == "__main__":
    main()
