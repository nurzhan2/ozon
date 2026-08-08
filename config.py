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

    Важно: проблемы с Google НЕ должны ронять сервис. Если ключа нет или он
    битый (например, в переменной остался шаблон из .env.example), сервис
    только предупреждает и отключает выгрузку — отчёты всё равно соберутся
    в файлы, а причина будет видна в логе.
    """
    global UPLOAD_TO_GOOGLE

    path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "google_service_account.json")
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()

    if not UPLOAD_TO_GOOGLE:
        return path                      # выгрузка выключена — ключ не нужен
    if not raw or os.path.exists(path):
        return path

    try:
        json.loads(raw)                  # проверяем, что это валидный JSON
    except json.JSONDecodeError as e:
        print(f"[config] GOOGLE_CREDENTIALS_JSON не разобран как JSON ({e}). "
              f"Похоже, в переменной остался шаблон из .env.example. "
              f"Выгрузка в Google отключена, отчёты будут собираться в файлы.")
        UPLOAD_TO_GOOGLE = False
        return path

    target = os.path.join(DATA_DIR if os.path.isdir(DATA_DIR) else ".",
                          "google_service_account.json")
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(raw)
        os.chmod(target, 0o600)
    except OSError as e:
        print(f"[config] Не удалось сохранить ключ Google ({e}). "
              f"Выгрузка отключена, отчёты будут собираться в файлы.")
        UPLOAD_TO_GOOGLE = False
        return path
    return target


GOOGLE_CREDENTIALS_FILE = _resolve_credentials()

# Папка на Google Диске с выгрузками из личного кабинета OZON.
# Внутри — по подпапке на магазин, имя подпапки должно совпадать с именем
# в OZON_STORES. Тот, у кого есть доступ к кабинету, кладёт туда файл, сбор
# берёт самый свежий. Пусто — источник просто не используется.
# Подробности и инструкция для клиента: ВЫГРУЗКА_ИЗ_КАБИНЕТА.md
GOOGLE_IMPORT_FOLDER = os.environ.get("GOOGLE_IMPORT_FOLDER", "").strip()
