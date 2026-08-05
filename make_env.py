#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Собирает значение переменной OZON_STORES из local_config.py.

Нужен, чтобы не собирать длинный JSON вручную: скрипт печатает готовую строку,
которую остаётся скопировать в Railway → Variables → OZON_STORES.

    cp local_config.example.py local_config.py   # вписать ключи
    python make_env.py

Строка печатается только в консоль и никуда не сохраняется — ключи не попадают
ни в файлы репозитория, ни в git.
"""

import sys
import json

try:
    import local_config
except ImportError:
    print("Не найден local_config.py.")
    print("Скопируйте шаблон и заполните ключи:")
    print("    cp local_config.example.py local_config.py")
    sys.exit(1)

stores = getattr(local_config, "STORES", None)
if not stores:
    print("В local_config.py не заполнен список STORES.")
    sys.exit(1)

problems = []
for s in stores:
    for field in ("name", "client_id", "api_key"):
        v = str(s.get(field, ""))
        if not v or v.startswith(("ЧИСЛОВОЙ", "API_", "РЕКЛАМНЫЙ")):
            problems.append(f"{s.get('name', '?')}: не заполнено «{field}»")

if problems:
    print("Сначала заполните значения в local_config.py:")
    for p in problems:
        print("  -", p)
    sys.exit(1)

value = json.dumps(stores, ensure_ascii=False, separators=(",", ":"))

print("=" * 70)
print("Скопируйте строку ниже в Railway → Variables → OZON_STORES")
print("(одной строкой, целиком, без переносов)")
print("=" * 70)
print()
print(value)
print()
print("=" * 70)
print(f"магазинов: {len(stores)} | длина строки: {len(value)} символов")
print("Проверьте, что переменная задана как Secret и репозиторий приватный.")
