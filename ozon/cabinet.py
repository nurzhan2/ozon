# -*- coding: utf-8 -*-
"""
Импорт выгрузок из личного кабинета OZON.

ЗАЧЕМ
    Четыре строки отчёта «Качественные показатели» — показы, корзина,
    % корзины и место в поиске — через API без подписки Premium Plus взять
    негде. Проверены все четыре пути: /v1/analytics/data режет метрики по
    тарифу, /v1/analytics/product-queries отвечает через раз и корзины не
    знает вовсе, /v1/search-queries требует Premium Pro и отдаёт рыночную
    статистику по запросу, а не по нашему товару, в /v1/report/* нужного
    типа отчёта нет.

    В кабинете эти цифры есть и выгружаются файлом. Поэтому: тот, у кого
    есть доступ к кабинету, раз в день кладёт выгрузку в общую папку
    Google Диска, а сбор её читает и заполняет строки. Пароли нигде не
    хранятся, автоматизация браузера не нужна.

ЧТО ЧИТАЕМ
    xlsx и csv. Колонки ищутся по названиям, а не по номерам: OZON меняет
    порядок и формулировки, жёсткая привязка к позиции сломается на первой
    же правке. Нужны колонка с датой, колонка с артикулом или SKU и хотя бы
    одна из числовых.

    Если колонки с датой нет — файл пропускается с внятным сообщением.
    Выгрузка без разбивки по дням для подневного отчёта бесполезна, а
    размазать период по дням значило бы выдумать числа.

ГДЕ ИЩЕМ
    1. Папка на Google Диске (GOOGLE_IMPORT_FOLDER) с подпапками по имени
       магазина — так один каталог обслуживает все пять.
    2. Локально: DATA_DIR/import/<Магазин>/ — для прогонов на своей машине.

    Берётся самый свежий файл в папке магазина.
"""

import io
import os
import csv
import re
import logging
from datetime import datetime

log = logging.getLogger("ozon.cabinet")

# Названия колонок, которые встречаются в выгрузках кабинета. Список
# намеренно широкий: у разных отчётов OZON формулировки расходятся
# («Показы» / «Показы всего» / «Показы в поиске и категории»).
COLUMNS = {
    "day": ("дата", "день", "date", "period", "период"),
    "offer_id": ("артикул", "ваш sku", "offer id", "offer_id", "код товара"),
    "sku": ("sku", "ozon id", "озон id", "ид товара", "идентификатор товара"),
    "views": ("показы", "показов", "показы всего", "просмотры", "hits_view",
              "views"),
    # «Уникальные посетители, всего» — это session_view в терминах API:
    # уникальные люди, а не показы. Идёт в строку «клики».
    "sessions": ("уникальные посетители, всего", "сессии", "сессий",
                 "уникальные посетители", "посетители", "session_view",
                 "клики", "переходы"),
    # Посетители именно карточки: нужны, чтобы развернуть конверсию в штуки.
    "sessions_pdp": ("уникальные посетители с просмотром карточки товара",
                     "с просмотром карточки", "session_view_pdp"),
    "tocart": ("в корзину", "корзина", "добавления в корзину",
               "добавлено в корзину", "hits_tocart", "add_to_cart"),
    # В выгрузке кабинета корзина часто есть только процентом.
    "conv_tocart": ("конверсия в корзину из карточки товара",
                    "конверсия в корзину", "conv_tocart"),
    "position": ("позиция", "место в поиске", "средняя позиция",
                 "position", "position_category"),
}

# Колонки, ради которых всё затевается. Файл без единой из них бесполезен.
VALUE_KEYS = ("views", "sessions", "sessions_pdp", "tocart", "conv_tocart",
              "position")

# Складываются при повторах; позиция и конверсия — нет, это доли и места.
SUM_KEYS = ("views", "sessions", "sessions_pdp", "tocart")


class CabinetImportError(Exception):
    pass


# ------------------------------------------------------------------ разбор

def _norm(s):
    """Название колонки к сравнимому виду: без регистра, пробелов и скобок."""
    s = str(s or "").strip().lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)", " ", s)
    return re.sub(r"[\s ]+", " ", s).strip()


# Порядок разбора шапки. Роли идут от самых узких к широким, и одна колонка
# достаётся только одной роли. Без этого «Конверсия в корзину из карточки
# товара» попадала и в conv_tocart, и в tocart — потому что содержит слова
# «в корзину», — и в отчёт вместо штук уезжала доля 0,37.
ROLE_ORDER = ("day", "offer_id", "sku", "conv_tocart", "sessions_pdp",
              "tocart", "views", "sessions", "position")


def _match_columns(header):
    """{наша_роль: индекс_колонки}. Точное совпадение важнее вхождения."""
    norm = [_norm(h) for h in header]
    found, used = {}, set()
    for role in ROLE_ORDER:
        names = COLUMNS[role]
        for i, h in enumerate(norm):          # сначала точное совпадение
            if i not in used and h in names:
                found[role], _ = i, used.add(i)
                break
        if role in found:
            continue
        for i, h in enumerate(norm):          # затем вхождение
            if i not in used and any(n in h for n in names):
                found[role], _ = i, used.add(i)
                break
    return found


def _num(v):
    """
    «1 234,5», «1,234.5» и «36,99%» — в число. Мусор — в ноль.

    Процент возвращается долей: 36,99% -> 0.3699. Иначе конверсия, которой
    в выгрузке кабинета заменяют абсолютную корзину, была бы завышена
    в сто раз.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    raw = str(v)
    pct = "%" in raw
    s = raw.strip().replace("\u00a0", "").replace(" ", "").replace("%", "")
    if "," in s and "." in s:
        s = s.replace(",", "")          # 1,234.5
    else:
        s = s.replace(",", ".")         # 1234,5
    try:
        num = float(s)
    except ValueError:
        return 0.0
    return num / 100.0 if pct else num


_DATE_PATTERNS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d-%m-%Y")


def _day(v):
    """Дата в 'YYYY-MM-DD'. Не разобралась — пустая строка."""
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if hasattr(v, "isoformat") and not isinstance(v, str):
        try:
            return v.isoformat()[:10]
        except Exception:
            return ""
    s = str(v).strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _rows_from_csv(data):
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim)]


def _rows_from_xlsx(data):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _find_header(rows):
    """
    Шапка не всегда в первой строке: выгрузки OZON любят пару строк
    с названием отчёта и периодом сверху. Ищем первую строку, в которой
    нашлись и дата, и хоть одна нужная величина.
    """
    for i, row in enumerate(rows[:15]):
        if not row:
            continue
        cols = _match_columns(row)
        if "day" in cols and any(k in cols for k in VALUE_KEYS):
            return i, cols
    return -1, {}


def parse_rows(rows, source=""):
    """
    Строки файла -> {ключ_товара: {день: {views, sessions, tocart, position}}}.
    Ключ — артикул, если он есть в файле, иначе sku.
    """
    head, cols = _find_header(rows)
    if head < 0:
        sample = _norm(" | ".join(str(c) for c in (rows[0] if rows else [])))
        if any("день" not in _norm(str(c)) for c in (rows[0] if rows else [])) \
                and rows:
            raise CabinetImportError(
                f"{source}: не нашёл колонку с датой. Нужна выгрузка С "
                f"РАЗБИВКОЙ ПО ДНЯМ — без неё подневный отчёт заполнить "
                f"нельзя. Первая строка файла: {sample[:200]}")
        raise CabinetImportError(f"{source}: не разобрал шапку файла")

    if "offer_id" not in cols and "sku" not in cols:
        raise CabinetImportError(
            f"{source}: в файле нет ни артикула, ни SKU — товары не с чем "
            f"сопоставить")

    out = {}
    bad_days = 0
    for row in rows[head + 1:]:
        if not row:
            continue

        def cell(role):
            i = cols.get(role)
            return row[i] if i is not None and i < len(row) else None

        day = _day(cell("day"))
        if not day:
            bad_days += 1
            continue
        key = str(cell("offer_id") or "").strip() or str(cell("sku") or "").strip()
        if not key or key.lower() in ("none", "итого", "total"):
            continue

        rec = out.setdefault(key, {}).setdefault(
            day, {r: 0.0 for r in VALUE_KEYS})
        for role in VALUE_KEYS:
            if role in cols:
                v = _num(cell(role))
                if role in SUM_KEYS:
                    rec[role] += v
                elif v:
                    # позиция и конверсия — доли и места, их не складывают
                    rec[role] = v

    # Корзину кабинет часто отдаёт только процентом «конверсия в корзину из
    # карточки товара». Штуки из него получаются умножением на посетителей
    # карточки — это арифметика над двумя отданными числами, а не догадка.
    # Но считается она ТОЛЬКО по карточке: добавления из поиска и каталога
    # сюда не попадают, поэтому число выйдет ниже кабинетного «в корзину».
    if "tocart" not in cols and "conv_tocart" in cols and "sessions_pdp" in cols:
        for days in out.values():
            for rec in days.values():
                rec["tocart"] = rec["conv_tocart"] * rec["sessions_pdp"]
        log.info("%s: колонки «в корзину» нет — считаю из конверсии по "
                 "карточке, число будет ниже кабинетного", source)

    if bad_days:
        log.debug("%s: строк без разобранной даты: %d", source, bad_days)
    if not out:
        raise CabinetImportError(f"{source}: файл разобран, но данных в нём нет")
    return out


def parse_file(data, name=""):
    """Байты файла -> разобранные строки. Формат по расширению."""
    low = name.lower()
    if low.endswith(".csv") or low.endswith(".txt"):
        rows = _rows_from_csv(data)
    elif low.endswith(".xlsx") or low.endswith(".xlsm"):
        rows = _rows_from_xlsx(data)
    elif data[:2] == b"PK":
        rows = _rows_from_xlsx(data)
    else:
        rows = _rows_from_csv(data)
    return parse_rows(rows, source=name or "файл")


# ------------------------------------------------------------------ источники

def _newest_local(folder):
    if not os.path.isdir(folder):
        return None
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith((".xlsx", ".xlsm", ".csv", ".txt"))
             and not f.startswith("~$")]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def load_local(store_name, data_dir):
    """Самый свежий файл из DATA_DIR/import/<Магазин>/."""
    path = _newest_local(os.path.join(data_dir, "import", store_name))
    if not path:
        return {}
    with open(path, "rb") as f:
        data = f.read()
    out = parse_file(data, os.path.basename(path))
    log.info("[%s] выгрузка кабинета: %s, товаров %d",
             store_name, os.path.basename(path), len(out))
    return out


def load_drive(store_name, folder_id, credentials_file):
    """
    Самый свежий файл из подпапки <Магазин> в папке Google Диска.

    Сервисный аккаунт тот же, что и для выгрузки отчётов, права на Диск у
    него уже есть — достаточно поделиться папкой с его почтой.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=["https://www.googleapis.com/auth/drive"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    q = (f"'{folder_id}' in parents and trashed = false and "
         f"mimeType = 'application/vnd.google-apps.folder'")
    subs = drive.files().list(q=q, fields="files(id,name)").execute().get("files", [])
    sub = next((s for s in subs if _norm(s["name"]) == _norm(store_name)), None)
    if not sub:
        log.info("[%s] на Диске нет подпапки с таким именем — пропускаю",
                 store_name)
        return {}

    q = f"'{sub['id']}' in parents and trashed = false"
    files = drive.files().list(
        q=q, orderBy="modifiedTime desc", pageSize=10,
        fields="files(id,name,mimeType,modifiedTime)").execute().get("files", [])
    files = [f for f in files if f["mimeType"] != "application/vnd.google-apps.folder"]
    if not files:
        log.info("[%s] подпапка на Диске пуста — пропускаю", store_name)
        return {}

    f = files[0]
    if f["mimeType"] == "application/vnd.google-apps.spreadsheet":
        data = drive.files().export(
            fileId=f["id"],
            mimeType="text/csv").execute()
        name = f["name"] + ".csv"
    else:
        data = drive.files().get_media(fileId=f["id"]).execute()
        name = f["name"]

    out = parse_file(data, name)
    log.info("[%s] выгрузка кабинета с Диска: %s (%s), товаров %d",
             store_name, name, f.get("modifiedTime", "")[:10], len(out))
    return out


def load(store_name, cfg):
    """
    Данные кабинета для магазина. Сначала Диск, если он настроен, потом
    локальная папка. Любая ошибка — предупреждение, а не падение сбора:
    отсутствие выгрузки не должно ронять остальные четыре отчёта.
    """
    folder = getattr(cfg, "GOOGLE_IMPORT_FOLDER", "") or ""
    creds = getattr(cfg, "GOOGLE_CREDENTIALS_FILE", "") or ""
    if folder and creds and os.path.exists(creds):
        try:
            data = load_drive(store_name, folder, creds)
            if data:
                return data
        except CabinetImportError as e:
            log.warning("[%s] выгрузку с Диска не разобрал: %s", store_name, e)
        except Exception as e:  # сеть, права, отозванный ключ
            log.warning("[%s] Google Диск недоступен (%s) — смотрю локально",
                        store_name, str(e)[:200])
    try:
        return load_local(store_name, getattr(cfg, "DATA_DIR", "data"))
    except CabinetImportError as e:
        log.warning("[%s] локальную выгрузку не разобрал: %s", store_name, e)
        return {}
