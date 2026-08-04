# -*- coding: utf-8 -*-
"""
Конфигурация проекта.

Читает настройки из ПЕРЕМЕННЫХ ОКРУЖЕНИЯ — так секреты (ключи OZON, доступ к
Google) не попадают в git и задаются в панели Railway. Если переменных нет,
подхватывается локальный файл local_config.py — удобно для отладки на своей
машине. Сам этот файл секретов не содержит и спокойно лежит в репозитории.

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
--------------------
OZON_STORES              JSON-список магазинов (обязательно). Пример:
                         [{"name":"ШТУЧКА","client_id":"96144576",
                           "api_key":"...","perf_client_id":"...",
                           "perf_client_secret":"..."}]
GOOGLE_CREDENTIALS_JSON  Содержимое JSON-ключа сервисного аккаунта целиком
GOOGLE_SHEET_CUMULATIVE  ID таблицы для отчёта 1
GOOGLE_SHEET_DOD         ID таблицы для отчёта 2
GOOGLE_SHEET_QUALITY     ID таблицы для отчёта 3
GOOGLE_SHEET_STOCKS      ID таблицы для отчёта 4
GOOGLE_SHEET_INTRADAY    ID таблицы для отчёта 5
GOOGLE_SHARE_WITH        Адреса через запятую, кому выдать доступ
UPLOAD_TO_GOOGLE         1 или 0 (по умолчанию 1)
ENABLE_PERFORMANCE       1 или 0 (по умолчанию 1)
CUMULATIVE_DAYS          0 — с 1-го числа месяца, 7 — скользящая неделя
DATA_DIR                 Папка для output/ и snapshots/ (на Railway — /data)
"""

import os
import json


def _bool(name, default=True):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "да")


def _int(name, default=0):
    try:
        return int(os.environ.get(name, "").strip())
    except (ValueError, AttributeError):
        return default


# ------------------------------------------------------------- магазины
def _load_stores():
    raw = os.environ.get("OZON_STORES", "").strip()
    if raw:
        try:
            stores = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"OZON_STORES содержит некорректный JSON: {e}\n"
                f"Ожидается список вида [{{\"name\":\"...\",\"client_id\":\"...\","
                f"\"api_key\":\"...\"}}]"
            )
        if not isinstance(stores, list) or not stores:
            raise SystemExit("OZON_STORES должен быть непустым списком магазинов.")
        for s in stores:
            for field in ("name", "client_id", "api_key"):
                if not s.get(field):
                    raise SystemExit(
                        f"В магазине {s.get('name', '?')} не заполнено поле «{field}»."
                    )
        return stores

    # запасной вариант — локальный файл, не попадающий в git
    try:
        import local_config
        return local_config.STORES
    except ImportError:
        raise SystemExit(
            "Не заданы магазины. Укажите переменную окружения OZON_STORES "
            "или создайте local_config.py на основе local_config.example.py."
        )


STORES = _load_stores()

# ------------------------------------------------------------- общие настройки
EXCLUDE_ARTICLE_MARKER = os.environ.get("EXCLUDE_ARTICLE_MARKER", "OUT")
ENABLE_PERFORMANCE = _bool("ENABLE_PERFORMANCE", True)
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Moscow")
CUMULATIVE_DAYS = _int("CUMULATIVE_DAYS", 0)
QUALITY_TOTAL_COLUMN = _bool("QUALITY_TOTAL_COLUMN", False)

# Папка с данными. На Railway монтируется постоянный диск в /data —
# это важно для snapshots/, иначе внутридневное сравнение обнулялось бы
# при каждом перезапуске сервиса.
DATA_DIR = os.environ.get("DATA_DIR", ".")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
SNAPSHOTS_DIR = os.path.join(DATA_DIR, "snapshots")

# ------------------------------------------------------------- Google
UPLOAD_TO_GOOGLE = _bool("UPLOAD_TO_GOOGLE", True)

GOOGLE_SHEETS = {
    "cumulative": os.environ.get("GOOGLE_SHEET_CUMULATIVE", "").strip(),
    "dod": os.environ.get("GOOGLE_SHEET_DOD", "").strip(),
    "quality": os.environ.get("GOOGLE_SHEET_QUALITY", "").strip(),
    "stocks": os.environ.get("GOOGLE_SHEET_STOCKS", "").strip(),
    "intraday": os.environ.get("GOOGLE_SHEET_INTRADAY", "").strip(),
}

GOOGLE_SHARE_WITH = [
    e.strip() for e in os.environ.get("GOOGLE_SHARE_WITH", "").split(",") if e.strip()
]


def _resolve_credentials():
    """
    Ключ сервисного аккаунта можно задать двумя способами: положить файл рядом
    с проектом или передать его содержимое переменной GOOGLE_CREDENTIALS_JSON
    (так удобнее на Railway). Во втором случае пишем во временный файл.
    """
    path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "google_service_account.json")
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw and not os.path.exists(path):
        target = os.path.join(DATA_DIR if os.path.isdir(DATA_DIR) else ".",
                              "google_service_account.json")
        try:
            json.loads(raw)          # проверяем, что это валидный JSON
        except json.JSONDecodeError as e:
            raise SystemExit(f"GOOGLE_CREDENTIALS_JSON — некорректный JSON: {e}")
        with open(target, "w", encoding="utf-8") as f:
            f.write(raw)
        os.chmod(target, 0o600)
        return target
    return path


GOOGLE_CREDENTIALS_FILE = _resolve_credentials()
