# -*- coding: utf-8 -*-
"""
Локальная конфигурация ДЛЯ ОТЛАДКИ НА СВОЕЙ МАШИНЕ.

    cp local_config.example.py local_config.py

и подставьте реальные ключи. Файл local_config.py добавлен в .gitignore и
в репозиторий не попадёт.

НА СЕРВЕРЕ (Railway) этот файл НЕ используется — там всё берётся из переменных
окружения, см. .env.example и config.py.

ГДЕ ВЗЯТЬ client_id (числовой ID продавца):
    Личный кабинет OZON -> Настройки -> Seller API. Рядом с Api-Key показано
    число. Оно же обычно является первой частью рекламного Client ID:
        96144205-1782903282482@advertising.performance.ozon.ru
        ^^^^^^^^
    Проверить можно скриптом check_access.py.
"""

STORES = [
    {
        "name": "СЕКРЕТЫ КРАСОТЫ",
        "client_id": "ЧИСЛОВОЙ_CLIENT_ID",
        "api_key": "API_КЛЮЧ_АНАЛИТИКИ",
        "perf_client_id": "РЕКЛАМНЫЙ_CLIENT_ID",
        "perf_client_secret": "РЕКЛАМНЫЙ_СЕКРЕТ",
    },
    {
        "name": "БЬЮТИФУЛ",
        "client_id": "ЧИСЛОВОЙ_CLIENT_ID",
        "api_key": "API_КЛЮЧ_АНАЛИТИКИ",
        "perf_client_id": "РЕКЛАМНЫЙ_CLIENT_ID",
        "perf_client_secret": "РЕКЛАМНЫЙ_СЕКРЕТ",
    },
    {
        "name": "ДАЙМОНД",
        "client_id": "ЧИСЛОВОЙ_CLIENT_ID",
        "api_key": "API_КЛЮЧ_АНАЛИТИКИ",
        "perf_client_id": "РЕКЛАМНЫЙ_CLIENT_ID",
        "perf_client_secret": "РЕКЛАМНЫЙ_СЕКРЕТ",
    },
    {
        "name": "ЛИНИЯ МЕЧТЫ",
        "client_id": "ЧИСЛОВОЙ_CLIENT_ID",
        "api_key": "API_КЛЮЧ_АНАЛИТИКИ",
        "perf_client_id": "РЕКЛАМНЫЙ_CLIENT_ID",
        "perf_client_secret": "РЕКЛАМНЫЙ_СЕКРЕТ",
    },
    {
        "name": "ШТУЧКА",
        "client_id": "ЧИСЛОВОЙ_CLIENT_ID",
        "api_key": "API_КЛЮЧ_АНАЛИТИКИ",
        "perf_client_id": "РЕКЛАМНЫЙ_CLIENT_ID",
        "perf_client_secret": "РЕКЛАМНЫЙ_СЕКРЕТ",
    },
]
