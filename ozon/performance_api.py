# -*- coding: utf-8 -*-
"""
Клиент OZON Performance API (реклама).

Документация: https://docs.ozon.ru/api/performance/
Базовый URL:  https://api-performance.ozon.ru
Авторизация:  OAuth client_credentials -> Bearer access_token.

Статистика в Performance API асинхронная:
  1) POST /api/client/statistics        -> получаем UUID отчёта
  2) GET  /api/client/statistics/{uuid} -> ждём статус OK
  3) GET  /api/client/statistics/report -> скачиваем CSV

ГЛАВНОЕ ОГРАНИЧЕНИЕ: 2000 запросов на рекламный аккаунт в сутки.
Один отчёт по пачке из 10 кампаний — это 1 POST + N опросов статуса + 1
скачивание, то есть 6-17 запросов. При 120 кампаниях полный проход по
магазину стоит около сотни запросов, а за сутки воркер делает такой проход
десяток раз (утренний пакет + промежуточные каждые 2 часа). Поэтому здесь
всё построено вокруг экономии запросов:

  * счётчик расхода за сутки лежит на диске и переживает перезапуск;
  * кампании, по которым OZON запрещает этот тип отчёта, попадают в
    постоянный чёрный список и больше не запрашиваются;
  * архивные кампании и кампании, закончившиеся до начала периода,
    отсеиваются до формирования пачек;
  * опрос статуса идёт с нарастающей паузой, а не каждые 3 секунды.

Если реклама не нужна — модуль можно не использовать (ENABLE_PERFORMANCE=0).
"""

import io
import os
import zipfile
import csv
import json
import time
import logging
import threading
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # Python < 3.9
    ZoneInfo = None

import requests

log = logging.getLogger("ozon.perf")

BASE_URL = "https://api-performance.ozon.ru"

# OZON принимает не больше 10 кампаний в одном запросе статистики
# ("Превышен лимит по количеству кампаний (максимум 10)").
MAX_CAMPAIGNS_PER_REQUEST = 10

# Верхняя граница на число кампаний в отчёте. 0 — без ограничения, берутся ВСЕ.
# По умолчанию именно 0: усечение молча искажает «рекламу» и «ДРР», а это те
# самые колонки, ради которых отчёт и собирается. Переменная оставлена как
# аварийный тормоз, если у магазина окажутся сотни кампаний.
MAX_CAMPAIGNS_TOTAL = int(os.environ.get("PERF_MAX_CAMPAIGNS", "0"))

# Сколько пачек собирать одновременно.
#
# ПО УМОЛЧАНИЮ 1, И МЕНЯТЬ ЭТО НЕ НАДО. Performance API разрешает ровно один
# активный запрос статистики на аккаунт: при попытке запустить второй он
# отвечает 429 «Превышен лимит активных запросов (максимум 1)». Проверено на
# боевом аккаунте — при четырёх потоках отвалились 4 пачки из 12.
PARALLEL_BATCHES = max(1, int(os.environ.get("PERF_PARALLEL", "1")))

# Сколько ждать готовности одного отчёта.
REPORT_TIMEOUT = float(os.environ.get("PERF_REPORT_TIMEOUT", "300"))

# Опрос статуса с нарастающей паузой. Фиксированные 3 секунды означали, что
# отчёт, готовящийся 45 секунд, стоил 15 запросов из суточных 2000. С ростом
# паузы тот же отчёт обходится в 7 запросов, а задержка растёт секунд на пять.
POLL_START = float(os.environ.get("PERF_POLL_START", "2"))
POLL_MAX = float(os.environ.get("PERF_POLL_MAX", "15"))
POLL_GROWTH = float(os.environ.get("PERF_POLL_GROWTH", "1.6"))

# Как часто напоминать в лог, что отчёт всё ещё готовится.
POLL_NOTICE = float(os.environ.get("PERF_POLL_NOTICE", "30"))

# Суточный лимит OZON и наш собственный потолок с запасом. Упереться в чужой
# лимит — значит получить 429 на середине сбора и потерять данные без предупре-
# ждения; свой потолок даёт остановиться заранее и написать об этом в лог.
OZON_DAILY_LIMIT = 2000
DAILY_BUDGET = int(os.environ.get("PERF_DAILY_BUDGET", "1500"))

# Через сколько часов после отказа OZON по суточному лимиту сделать пробную
# попытку. Их окно считается не по московской полуночи, так что ждать до утра
# значит терять день рекламы там, где лимит уже отпустило. 0 — не пробовать.
BLOCK_RETRY_HOURS = float(os.environ.get("PERF_BLOCK_RETRY_HOURS", "3"))

# Куда складывать счётчик запросов и чёрный список кампаний. На Railway
# DATA_DIR=/data — это постоянный диск, файлы переживают деплой.
CACHE_DIR = (os.environ.get("PERF_CACHE_DIR")
             or os.path.join(os.environ.get("DATA_DIR", "."), "cache"))

# Статусы кампаний, которые не попадают в отчёт. По умолчанию только архивные:
# именно на них OZON отвечает «generation of this type of report is forbidden».
# Остановленные и неактивные оставляем — они могли тратить деньги в начале
# периода, и выкинуть их значит занизить «рекламу» и «ДРР».
SKIP_STATES = {s.strip().upper()
               for s in os.environ.get("PERF_SKIP_STATES", "ARCHIVED").split(",")
               if s.strip()}

# Отсев давно не менявшихся неработающих кампаний.
#
# В кабинетах их много: у «Бьютифул» 105 неактивных из 221, часть создана
# в 2024 году и с тех пор не трогалась. Такая кампания не могла тратить бюджет
# в отчётном периоде, но пачку под себя занимает и стоит запросов.
#
# ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО (0). Включать — только посмотрев в perf_audit.py,
# сколько кампаний фильтр уносит: ошибка здесь занижает «рекламу» и «ДРР»
# молча, а это ровно те колонки, ради которых отчёт и собирается.
# Значение — запас в днях до начала периода: при 7 отсеиваются кампании,
# не менявшиеся дольше чем за неделю до его начала.
STALE_DAYS = int(os.environ.get("PERF_STALE_DAYS", "0"))

# Искать ли виновные кампании делением пачки пополам при отказе «отчёт этого
# типа запрещён». Один раз дорого, зато результат сохраняется на диск.
ISOLATE_FORBIDDEN = (os.environ.get("PERF_ISOLATE_FORBIDDEN", "1").strip().lower()
                     not in ("0", "false", "no"))
ISOLATE_MAX = int(os.environ.get("PERF_ISOLATE_MAX", "120"))

_FILE_LOCK = threading.Lock()


def _num(x):
    """'1 234,56' -> 1234.56"""
    if x is None:
        return 0.0
    x = str(x).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(x)
    except ValueError:
        return 0.0


def _norm_date(x):
    """Приводит дату из CSV к 'YYYY-MM-DD' (принимает 'ДД.ММ.ГГГГ' и ISO)."""
    s = str(x or "").strip()
    if not s:
        return ""
    if "." in s:
        parts = s.split(".")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return s[:10]


def _msk_date():
    """Дата по Москве — в этих сутках OZON считает свои 2000 запросов."""
    if ZoneInfo:
        return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
    return datetime.utcnow().strftime("%Y-%m-%d")


def _safe_name(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(s))[:64]


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.debug("не удалось записать %s: %s", path, e)


class PerformanceAPIError(Exception):
    pass


# По каким словам узнаём строку заголовков внутри CSV. Перед ней OZON иногда
# кладёт строку с названием кампании — её надо пропустить, иначе заголовками
# станет название, а данные потеряются.
_HEADER_HINTS = ("sku", "артикул", "дата", "date", "расход", "spend", "ozon id")


def _report_parts(content, name="", tag=""):
    """
    Достаёт из ответа OZON все CSV отчёта.

    Отчёт по пачке кампаний приходит ZIP-архивом, и внутри лежит ОТДЕЛЬНЫЙ
    файл на каждую кампанию: на пачку из десяти — десять csv с именами вида
    33016625_01.08.2026-05.08.2026.csv. Брать только первый нельзя: так
    теряется девять десятых расхода, причём незаметно — строки-то есть.

    Возвращает список текстов (по одному на файл). Голый CSV без архива —
    список из одного элемента.
    """
    if content[:4] != b"PK\x03\x04":
        return [content.decode("utf-8-sig", errors="replace")]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            csvs = [n for n in names if n.lower().endswith(".csv")] or names
            if not csvs:
                raise PerformanceAPIError(f"[{name}] {tag}архив отчёта пуст")
            return [z.read(n).decode("utf-8-sig", errors="replace") for n in csvs]
    except zipfile.BadZipFile as e:
        raise PerformanceAPIError(f"[{name}] {tag}битый архив отчёта: {e}")


def _rows_from_csv(text):
    """
    Разбирает один CSV отчёта в список словарей.

    newline="" обязателен: без него StringIO режет текст только по '\n',
    в конце строк остаётся '\r', и csv падает с «new-line character seen in
    unquoted field». Именно так весь сбор рекламы и обвалился на первом
    боевом прогоне.
    """
    lines = text.splitlines(keepends=True)
    # ищем строку заголовков: до неё может стоять название кампании
    head = 0
    for i, line in enumerate(lines[:5]):
        low = line.lower()
        if ";" in line and any(h in low for h in _HEADER_HINTS):
            head = i
            break
    body = "".join(lines[head:])
    if not body.strip():
        return []
    reader = csv.DictReader(io.StringIO(body, newline=""), delimiter=";")
    return [row for row in reader]


class PerformanceQuotaError(PerformanceAPIError):
    """Суточный лимит запросов исчерпан — до завтра просить бесполезно."""
    pass


class PerformanceForbiddenReport(PerformanceAPIError):
    """OZON отказал в отчёте по этому набору кампаний (400 InvalidArgument)."""
    pass


class PerformanceAPI:
    def __init__(self, client_id, client_secret, name="", timeout=60):
        self.client_id = client_id
        self.client_secret = client_secret
        self.name = name
        self.timeout = timeout
        self.session = requests.Session()
        self._token = None
        self._token_exp = 0
        # Пачки кампаний могут идти в несколько потоков через одну сессию.
        # Обновление токена пишет и в self._token, и в общие заголовки сессии,
        # поэтому обёрнуто замком: иначе два потока разом полезут за токеном
        # и один затрёт заголовок другого на полуслове.
        self._auth_lock = threading.Lock()

        self._campaigns_cache = None
        self._campaigns_logged = False
        self._quota_hit = False
        self._isolate_left = ISOLATE_MAX
        self._requests_run = 0
        self._pending_writes = 0
        self._probed_at = 0.0
        self._probe_allowed = False
        # У отчёта может не оказаться колонки с датой. Тогда расход нельзя
        # разложить по дням, а значит и нарезать кэш на подпериоды.
        self.last_spend_dated = True

        tag = _safe_name(client_id)
        self._usage_path = os.path.join(CACHE_DIR, f"perf_usage_{tag}.json")
        self._forbid_path = os.path.join(CACHE_DIR, f"perf_forbidden_{tag}.json")
        self._forbidden = set(str(x) for x in
                              _read_json(self._forbid_path, {}).get("ids", []))
        self._usage = self._load_usage()

        if self._forbidden:
            log.info("[%s] в чёрном списке кампаний: %d (отчёт по ним запрещён OZON)",
                     self.name, len(self._forbidden))
        spent = self._usage.get("count", 0)
        if spent:
            log.info("[%s] за сегодня уже израсходовано запросов рекламы: %d из %d",
                     self.name, spent, DAILY_BUDGET or OZON_DAILY_LIMIT)
        if self._usage.get("blocked"):
            log.warning("[%s] сбор рекламы приостановлен: %s",
                        self.name, self._block_reason())

    # ---------------------------------------------------------- учёт запросов
    @staticmethod
    def _fresh_usage():
        return {"date": _msk_date(), "count": 0, "blocked": False,
                "blocked_at": 0, "reason": "", "own_limit": False}

    def _load_usage(self):
        u = _read_json(self._usage_path, {})
        if u.get("date") != _msk_date():
            return self._fresh_usage()
        for k, v in self._fresh_usage().items():
            u.setdefault(k, v)
        return u

    def _block_reason(self):
        """Человеческое объяснение, почему сбор стоит и когда снова попробуем."""
        why = self._usage.get("reason") or "суточный лимит запросов исчерпан"
        if self._usage.get("own_limit"):
            return (f"{why}. Это НАШ потолок, а не отказ OZON: до конца суток "
                    f"по Москве запросов больше не будет")
        at = float(self._usage.get("blocked_at") or 0)
        if at and BLOCK_RETRY_HOURS:
            waited = (time.time() - at) / 3600.0
            left = max(0.0, BLOCK_RETRY_HOURS - waited)
            return (f"{why}. Отказ пришёл от OZON {waited:.1f} ч назад; "
                    f"пробную попытку сделаем через {left:.1f} ч")
        return why

    def _save_usage(self, force=False):
        self._pending_writes += 1
        if force or self._pending_writes >= 5:
            self._pending_writes = 0
            _write_json(self._usage_path, self._usage)

    def _count_request(self):
        with _FILE_LOCK:
            if self._usage.get("date") != _msk_date():
                self._usage = self._fresh_usage()
                self._quota_hit = False
                self._isolate_left = ISOLATE_MAX
            self._usage["count"] += 1
            self._requests_run += 1
            self._save_usage()

    def _mark_quota_exhausted(self, reason="", own_limit=False):
        with _FILE_LOCK:
            self._usage["blocked"] = True
            self._usage["blocked_at"] = time.time()
            self._usage["reason"] = reason
            self._usage["own_limit"] = bool(own_limit)
            self._save_usage(force=True)
        self._probe_allowed = False
        self._quota_hit = True
        if own_limit:
            log.error("[%s] ОСТАНОВЛЕНО НАШИМ ПОТОЛКОМ: %s. "
                      "До конца суток по Москве реклама собираться не будет; "
                      "«реклама» и «ДРР» в отчётах будут занижены.",
                      self.name, reason)
        else:
            log.error("[%s] OZON ОТКАЗАЛ ПО СУТОЧНОМУ ЛИМИТУ (%s). "
                      "Пробную попытку сделаем через %.0f ч; до тех пор "
                      "«реклама» и «ДРР» будут занижены.",
                      self.name, reason or "429", BLOCK_RETRY_HOURS)

    def _clear_block(self):
        """Пробный запрос прошёл — значит на стороне OZON счётчик отпустило."""
        with _FILE_LOCK:
            self._usage["blocked"] = False
            self._usage["blocked_at"] = 0
            self._usage["reason"] = ""
            self._usage["own_limit"] = False
            self._save_usage(force=True)
        self._probe_allowed = False
        self._quota_hit = False
        log.info("[%s] лимит OZON отпустило — продолжаю сбор рекламы", self.name)

    def _probing(self):
        """Идёт ли сейчас разрешённая пробная попытка."""
        return self._probe_allowed or self._can_probe()

    def _can_probe(self):
        """
        Разрешить одну пробную попытку после отказа OZON.

        Суточное окно OZON считается не по московской полуночи (это видно по
        тому, что отказ доживал до следующих суток нашего счётчика), поэтому
        держать сбор выключенным до утра неправильно: можно потерять день
        рекламы там, где лимит уже отпустило. Попытка стоит одного запроса,
        а при повторном отказе блокировка ставится заново.

        Свой собственный потолок пробой не лечится — он на то и потолок.
        """
        if self._usage.get("own_limit") or not BLOCK_RETRY_HOURS:
            return False
        if self._probed_at and (time.time() - self._probed_at) < BLOCK_RETRY_HOURS * 3600:
            return False

        at = float(self._usage.get("blocked_at") or 0)
        if not at:
            # Отметки времени нет: либо файл счётчика писала прошлая версия,
            # либо его правили руками. Ждать в такой ситуации нечего — иначе
            # магазин остаётся выключенным навсегда, что и случилось
            # с «Секретами красоты». Пробуем сразу.
            self._probed_at = time.time()
            self._probe_allowed = True
            log.info("[%s] блокировка без отметки времени — пробую один запрос",
                     self.name)
            return True

        waited = time.time() - at
        if waited < BLOCK_RETRY_HOURS * 3600:
            return False
        self._probed_at = time.time()
        self._probe_allowed = True
        log.info("[%s] с отказа OZON по лимиту прошло %.1f ч — пробую один запрос",
                 self.name, waited / 3600.0)
        return True

    def _guard_quota(self):
        # Свой потолок проверяем первым: он окончательный на эти сутки.
        if DAILY_BUDGET and self._usage.get("count", 0) >= DAILY_BUDGET:
            if not self._usage.get("blocked"):
                self._mark_quota_exhausted(
                    f"израсходован бюджет PERF_DAILY_BUDGET={DAILY_BUDGET} "
                    f"(лимит OZON — {OZON_DAILY_LIMIT})", own_limit=True)
            self._quota_hit = True
            raise PerformanceQuotaError(
                f"[{self.name}] израсходован суточный бюджет запросов "
                f"({DAILY_BUDGET})"
            )

        if self._quota_hit or self._usage.get("blocked"):
            if self._probing():
                return
            self._quota_hit = True
            raise PerformanceQuotaError(
                f"[{self.name}] {self._block_reason()}"
            )

    def requests_left(self):
        limit = DAILY_BUDGET or OZON_DAILY_LIMIT
        return max(0, limit - self._usage.get("count", 0))

    # ------------------------------------------------------------ авторизация
    def _auth(self):
        with self._auth_lock:
            self._auth_locked()

    def _auth_locked(self):
        if self._token and time.time() < self._token_exp - 60:
            return
        self._count_request()
        r = self.session.post(
            BASE_URL + "/api/client/token",
            json={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise PerformanceAPIError(
                f"[{self.name}] Не удалось получить токен рекламы "
                f"(HTTP {r.status_code}): {r.text[:300]}"
            )
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 1800))
        self.session.headers.update({"Authorization": f"Bearer {self._token}"})

    # --------------------------------------------------------------- запросы
    # Временные отказы повторяются с нарастающей паузой: терять из-за них целую
    # пачку кампаний (а с ней часть расхода на рекламу) незачем. А вот
    # постоянные отказы — суточный лимит и запрет отчёта — повторять нельзя:
    # каждая попытка тратит запрос из тех же 2000.
    RETRY_CODES = (429, 500, 502, 503, 504)
    RETRIES = 6

    @staticmethod
    def _is_daily_limit(body):
        low = body.lower()
        return ("дневн" in low or "суточн" in low or "daily" in low) and "лимит" in low \
            or "daily limit" in low

    @staticmethod
    def _is_forbidden_report(body):
        low = body.lower()
        return ("forbidden for the transferred list" in low
                or "generation of this type of report is forbidden" in low)

    def _request(self, method, path, ok_codes, **kw):
        last = None
        for attempt in range(1, self.RETRIES + 1):
            self._guard_quota()
            self._auth()
            self._count_request()
            r = self.session.request(method, BASE_URL + path,
                                     timeout=self.timeout, **kw)
            if r.status_code in ok_codes:
                if self._usage.get("blocked"):
                    self._clear_block()
                return r

            body = r.text[:300]
            last = f"HTTP {r.status_code}: {body}"

            # Суточный лимит. Повторы бессмысленны и только жгут остаток.
            if r.status_code == 429 and self._is_daily_limit(body):
                self._mark_quota_exhausted("ответ OZON: 429")
                raise PerformanceQuotaError(f"[{self.name}] {method} {path} {last}")

            # Этот набор кампаний не поддерживает такой отчёт. Ошибка
            # постоянная: повторять нельзя, надо искать виновных.
            if r.status_code == 400 and self._is_forbidden_report(body):
                raise PerformanceForbiddenReport(f"[{self.name}] {method} {path} {last}")

            if r.status_code not in self.RETRY_CODES or attempt == self.RETRIES:
                break

            # «Превышен лимит активных запросов» означает, что предыдущий отчёт
            # ещё готовится. Частым опросом делу не поможешь — нужно ждать,
            # пока освободится единственный слот, поэтому пауза длиннее.
            busy = "активных запросов" in body
            pause = min(10 * attempt, 60) if busy else min(3 * attempt, 15)
            log.debug("[%s] %s %s -> %d, повтор через %.0f с",
                      self.name, method, path, r.status_code, pause)
            time.sleep(pause)
        raise PerformanceAPIError(f"[{self.name}] {method} {path} {last}")

    def _get(self, path, **kw):
        return self._request("GET", path, (200,), **kw)

    def _post(self, path, payload):
        return self._request("POST", path, (200, 202), json=payload).json()

    # ------------------------------------------------------------- кампании
    @staticmethod
    def _state_of(c):
        s = str(c.get("state") or c.get("status") or "").upper()
        return s.replace("CAMPAIGN_STATE_", "").strip() or "?"

    @staticmethod
    def _ended_before(c, date_from):
        """Кампания завершилась до начала периода — тратить на неё запрос незачем."""
        raw = c.get("toDate") or c.get("dateTo") or c.get("to_date") or ""
        end = _norm_date(raw)
        return bool(end) and len(end) == 10 and end < str(date_from)

    @staticmethod
    def last_touch(c):
        """
        Самая поздняя дата, какая известна про кампанию. Берём максимум из всех
        полей: если хоть одно позже начала периода, кампания могла быть живой.
        """
        best = ""
        for key in ("updatedAt", "toDate", "fromDate", "createdAt",
                    "updated_at", "created_at", "dateTo", "dateFrom"):
            v = _norm_date(c.get(key))
            if len(v) == 10 and v > best:
                best = v
        return best

    @classmethod
    def is_stale(cls, c, date_from, stale_days):
        """
        Кампания не работает и давно не менялась — в отчётном периоде она
        тратить не могла. RUNNING не трогаем никогда: кампания может крутиться
        годами без единой правки, и updatedAt у неё старый.
        """
        if not stale_days or not date_from:
            return False
        if cls._state_of(c) == "RUNNING":
            return False
        touch = cls.last_touch(c)
        if not touch:
            return False
        try:
            cutoff = date.fromisoformat(str(date_from)[:10]) - timedelta(days=stale_days)
        except ValueError:
            return False
        return touch < cutoff.isoformat()

    def _mark_forbidden(self, ids):
        ids = [str(i) for i in ids]
        with _FILE_LOCK:
            self._forbidden.update(ids)
            _write_json(self._forbid_path, {
                "ids": sorted(self._forbidden),
                "updated": _msk_date(),
                "note": "кампании, по которым OZON запрещает отчёт /api/client/statistics",
            })

    def campaigns(self, date_from=None, date_to=None, only_active=True):
        """
        GET /api/client/campaign -> список ID кампаний, годных для отчёта.

        Отсеиваются архивные (именно они дают 400 «report is forbidden»),
        кампании из чёрного списка и завершившиеся до начала периода.
        Остановленные и неактивные остаются: они могли тратить бюджет в
        начале периода.
        """
        if self._campaigns_cache is None:
            data = self._get("/api/client/campaign").json()
            self._campaigns_cache = data.get("list") or data.get("campaigns") or []
        items = self._campaigns_cache

        by_state = {}
        for c in items:
            st = self._state_of(c)
            by_state[st] = by_state.get(st, 0) + 1
        if not self._campaigns_logged:
            self._campaigns_logged = True
            log.info("[%s] кампаний в кабинете %d (%s)", self.name, len(items),
                     ", ".join(f"{k}={v}" for k, v in sorted(by_state.items())))

        kept = []
        dropped_state = dropped_black = dropped_period = dropped_stale = 0
        all_ids = []
        for c in items:
            cid = str(c.get("id") or c.get("campaignId") or "").strip()
            if not cid:
                continue
            all_ids.append(cid)
            if cid in self._forbidden:
                dropped_black += 1
                continue
            if only_active and self._state_of(c) in SKIP_STATES:
                dropped_state += 1
                continue
            if date_from and self._ended_before(c, date_from):
                dropped_period += 1
                continue
            if self.is_stale(c, date_from, STALE_DAYS):
                dropped_stale += 1
                continue
            kept.append(cid)

        if not kept and all_ids:
            # Статусы не распознались — лучше собрать лишнее, чем ничего.
            kept = [i for i in all_ids if i not in self._forbidden]
            log.warning("[%s] ни одна кампания не прошла фильтр по статусу — "
                        "беру все %d", self.name, len(kept))
        elif dropped_state or dropped_black or dropped_period or dropped_stale:
            log.info("[%s] в отчёт пойдут %d кампаний: пропущено %d по статусу, "
                     "%d из чёрного списка, %d завершились до начала периода, "
                     "%d не работают и давно не менялись",
                     self.name, len(kept), dropped_state, dropped_black,
                     dropped_period, dropped_stale)
        if dropped_stale:
            log.warning("[%s] отсев по PERF_STALE_DAYS=%d убрал %d кампаний — "
                        "если «реклама» просела, поставьте PERF_STALE_DAYS=0",
                        self.name, STALE_DAYS, dropped_stale)
        return kept

    # ------------------------------------------------------------ статистика
    def statistics(self, date_from, date_to, campaign_ids=None, group_by="DATE"):
        """
        Запрашивает статистику и дожидается готовности отчёта.
        Возвращает список dict-строк CSV (ключи — заголовки OZON, могут
        отличаться по локали: 'Расход, руб.', 'Клики', 'Показы', и т.п.).
        date_from/date_to: 'YYYY-MM-DD'.
        """
        if (self._quota_hit or self._usage.get("blocked")) and not self._probing():
            log.error("[%s] реклама пропущена: %s", self.name, self._block_reason())
            return []

        if campaign_ids is None:
            campaign_ids = self.campaigns(date_from, date_to)
        else:
            campaign_ids = [str(c) for c in campaign_ids
                            if str(c) not in self._forbidden]
        if not campaign_ids:
            return []

        if MAX_CAMPAIGNS_TOTAL and len(campaign_ids) > MAX_CAMPAIGNS_TOTAL:
            log.warning("[%s] кампаний %d, в отчёт войдут первые %d "
                        "(ограничение PERF_MAX_CAMPAIGNS) — остальные пропущены",
                        self.name, len(campaign_ids), MAX_CAMPAIGNS_TOTAL)
            campaign_ids = campaign_ids[:MAX_CAMPAIGNS_TOTAL]

        if len(campaign_ids) <= MAX_CAMPAIGNS_PER_REQUEST:
            # Одна пачка обрабатывается так же, как любая из многих: отказ
            # логируется и возвращается пустой результат. Иначе у магазина
            # с десятком кампаний ошибка летела наружу, а у магазина с сотней
            # та же самая ошибка тихо превращалась в предупреждение.
            try:
                return self._batch_isolating(date_from, date_to, campaign_ids, group_by)
            except PerformanceQuotaError:
                log.error("[%s] сбор рекламы прерван: %s",
                          self.name, self._block_reason())
                return []
            except Exception as e:
                log.warning("[%s] единственная пачка (кампании %s) пропущена: "
                            "%s: %s — «реклама» и «ДРР» будут занижены",
                            self.name, ",".join(str(c) for c in campaign_ids),
                            type(e).__name__, e)
                return []

        # OZON не принимает больше 10 кампаний за раз — идём пачками
        chunks = [campaign_ids[i:i + MAX_CAMPAIGNS_PER_REQUEST]
                  for i in range(0, len(campaign_ids), MAX_CAMPAIGNS_PER_REQUEST)]
        workers = min(PARALLEL_BATCHES, len(chunks))
        log.info("[%s] кампаний %d -> %d пачек по %d%s; запросов сегодня "
                 "израсходовано %d, остаток %d",
                 self.name, len(campaign_ids), len(chunks),
                 MAX_CAMPAIGNS_PER_REQUEST,
                 "" if workers == 1 else f", в {workers} потока(ов)",
                 self._usage.get("count", 0), self.requests_left())

        started = time.monotonic()
        req0 = self._requests_run
        rows, failed, quota = [], 0, False

        if workers == 1:
            # Последовательно и без пула: так предупреждение о неудачной пачке
            # печатается до начала следующей, а не после неё.
            for n, chunk in enumerate(chunks, 1):
                try:
                    rows.extend(self._batch_isolating(
                        date_from, date_to, chunk, group_by, f"{n}/{len(chunks)}"))
                except PerformanceQuotaError:
                    quota = True
                    log.error("[%s] сбор рекламы прерван на пачке %d из %d: "
                              "суточный лимит запросов исчерпан",
                              self.name, n, len(chunks))
                    break
                # Ловим любое исключение, а не только своё: сломанный CSV в
                # одной пачке не повод потерять рекламу по всему магазину.
                except Exception as e:
                    failed += 1
                    log.warning("[%s] пачка %d/%d (кампании %s) пропущена: %s: %s",
                                self.name, n, len(chunks), ",".join(chunk),
                                type(e).__name__, e)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                tasks = {
                    pool.submit(self._batch_isolating, date_from, date_to,
                                chunk, group_by, f"{n}/{len(chunks)}"): (n, chunk)
                    for n, chunk in enumerate(chunks, 1)
                }
                for task in as_completed(tasks):
                    n, chunk = tasks[task]
                    try:
                        rows.extend(task.result())
                    except PerformanceQuotaError:
                        quota = True
                    except Exception as e:
                        failed += 1
                        log.warning("[%s] пачка %d/%d (кампании %s) пропущена: %s: %s",
                                    self.name, n, len(chunks), ",".join(chunk),
                                    type(e).__name__, e)

        if failed:
            log.warning("[%s] не собрано пачек: %d из %d — "
                        "«реклама» и «ДРР» по этим кампаниям будут занижены",
                        self.name, failed, len(chunks))
        if quota:
            log.warning("[%s] часть пачек не собрана из-за суточного лимита — "
                        "«реклама» и «ДРР» занижены", self.name)
        log.info("[%s] статистика рекламы собрана за %.0f с, строк: %d, "
                 "запросов: %d (за сегодня %d из %d)",
                 self.name, time.monotonic() - started, len(rows),
                 self._requests_run - req0, self._usage.get("count", 0),
                 DAILY_BUDGET or OZON_DAILY_LIMIT)
        return rows

    def _batch_isolating(self, date_from, date_to, chunk, group_by="DATE", label=""):
        """
        Пачка с поиском виновных кампаний.

        Если OZON отвечает «отчёт этого типа запрещён для переданного списка
        кампаний», делим пачку пополам и повторяем: так одна плохая кампания
        не уносит с собой девять нормальных. Найденные заносятся в чёрный
        список на диске, и в следующие разы их даже не пробуем.
        """
        try:
            return self._statistics_batch(date_from, date_to, chunk, group_by, label)
        except PerformanceForbiddenReport as e:
            if len(chunk) == 1:
                self._mark_forbidden(chunk)
                log.warning("[%s] кампания %s не поддерживает этот отчёт — "
                            "занесена в чёрный список", self.name, chunk[0])
                return []
            if not ISOLATE_FORBIDDEN:
                raise
            if self._isolate_left <= 0:
                log.warning("[%s] пачка %s: отчёт запрещён, но лимит на поиск "
                            "виновных кампаний исчерпан — пропускаю (продолжу "
                            "в следующий запуск)", self.name, label or "?")
                return []
            self._isolate_left -= 1
            mid = len(chunk) // 2
            log.info("[%s] пачка %s: отчёт запрещён, делю %d кампаний на %d и %d, "
                     "чтобы не потерять рабочие",
                     self.name, label or "?", len(chunk), mid, len(chunk) - mid)
            out = []
            for part, suffix in ((chunk[:mid], "a"), (chunk[mid:], "b")):
                try:
                    out.extend(self._batch_isolating(
                        date_from, date_to, part, group_by, f"{label}{suffix}"))
                except PerformanceQuotaError:
                    raise
                except Exception as sub:
                    log.warning("[%s] пачка %s%s (кампании %s) пропущена: %s: %s",
                                self.name, label or "?", suffix, ",".join(part),
                                type(sub).__name__, sub)
            return out

    def _statistics_batch(self, date_from, date_to, campaign_ids,
                          group_by="DATE", label=""):
        """Один отчёт по пачке не больше MAX_CAMPAIGNS_PER_REQUEST кампаний."""
        tag = f"пачка {label}: " if label else ""
        payload = {
            "campaigns": [str(c) for c in campaign_ids],
            "from": f"{date_from}T00:00:00.000Z",
            "to": f"{date_to}T23:59:59.000Z",
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        }
        log.info("[%s] %sзапрашиваю отчёт по %d кампаниям",
                 self.name, tag, len(campaign_ids))

        # Отсчёт идёт отсюда, а не от получения UUID: в _post может уйти
        # несколько минут на повторы при «лимите активных запросов», и раньше
        # это время не попадало в «готово за N с» — в логе стояло «за 7 с»
        # там, где по часам прошло больше минуты.
        started = time.monotonic()

        resp = self._post("/api/client/statistics", payload)
        uuid = resp.get("UUID") or resp.get("uuid")
        if not uuid:
            raise PerformanceAPIError(f"[{self.name}] statistics: не получен UUID: {resp}")

        # Ожидание готовности. Ограничение по времени, а не по числу опросов:
        # когда пачек много, OZON ставит отчёты в очередь и ждать приходится
        # дольше, чем при одиночном запросе. Пауза между опросами растёт —
        # каждый опрос стоит запроса из суточных 2000.
        deadline = started + REPORT_TIMEOUT
        next_notice = started + POLL_NOTICE
        pause = POLL_START
        while time.monotonic() < deadline:
            st = self._get(f"/api/client/statistics/{uuid}").json()
            state = (st.get("state") or st.get("status") or "").upper()
            if state in ("OK", "SUCCESS", "DONE"):
                break
            if state in ("ERROR", "FAILED"):
                raise PerformanceAPIError(f"[{self.name}] отчёт рекламы завершился с ошибкой: {st}")
            now = time.monotonic()
            if now >= next_notice:
                log.info("[%s] %sотчёт ещё готовится, ждём %.0f с (статус %s)",
                         self.name, tag, now - started, state or "?")
                next_notice = now + POLL_NOTICE
            time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
            pause = min(POLL_MAX, pause * POLL_GROWTH)
        else:
            raise PerformanceAPIError(
                f"[{self.name}] отчёт рекламы не готов за {REPORT_TIMEOUT:.0f} с "
                f"(кампании {','.join(str(c) for c in campaign_ids)})"
            )

        # скачивание отчёта
        r = self._get("/api/client/statistics/report", params={"UUID": uuid})
        parts = _report_parts(r.content, self.name, tag)

        # Каждый файл архива — отдельная кампания со своей шапкой,
        # поэтому разбираем их по одному и складываем.
        out = []
        for part in parts:
            out.extend(_rows_from_csv(part))
        if len(parts) > 1:
            log.debug("[%s] %sфайлов в архиве: %d, строк суммарно: %d",
                      self.name, tag, len(parts), len(out))
        log.info("[%s] %sготово за %.0f с, строк %d",
                 self.name, tag, time.monotonic() - started, len(out))
        return out

    def spend_by_product_day(self, date_from, date_to):
        """
        Расход рекламы в разрезе ТОВАР × ДЕНЬ — нужен для колонок «реклама» и «ДРР».

        Запрашивает статистику с группировкой по дням и разбирает CSV: ищет
        колонки с артикулом/SKU/названием, датой и расходом. Названия колонок у
        OZON зависят от типа кампании, поэтому поиск идёт по подстрокам.

        Возвращает: {ключ_товара: {'YYYY-MM-DD': расход_руб}}
        где ключ_товара — sku (строкой) либо артикул, что нашлось в отчёте.
        """
        rows = self.statistics(date_from, date_to, group_by="DATE")
        out = {}
        dated = True
        for row in rows:
            keys = {(k or "").lower(): (k or "") for k in row}

            def find(*subs, exclude=()):
                for kl, orig in keys.items():
                    if any(s in kl for s in subs) and not any(e in kl for e in exclude):
                        return orig
                return None

            k_sku = find("sku", "артикул", "ozon id")
            k_date = find("дата", "date", "день")
            k_spend = find("расход", "spend", "cost", "затрат")
            if not (k_sku and k_spend):
                continue
            sku = str(row.get(k_sku) or "").strip()
            if not sku:
                continue
            if k_date:
                day = _norm_date(row.get(k_date)) or str(date_from)
            else:
                # Дат в отчёте нет — весь расход валится на первый день периода.
                # Нарезать такой результат на подпериоды нельзя.
                dated = False
                day = str(date_from)
            out.setdefault(sku, {})
            out[sku][day] = out[sku].get(day, 0.0) + _num(row.get(k_spend))

        self.last_spend_dated = dated
        if rows and not dated:
            log.warning("[%s] в отчёте рекламы нет колонки с датой — расход "
                        "отнесён на %s целиком", self.name, date_from)
        return out

    @staticmethod
    def aggregate_totals(rows):
        """
        Грубое агрегирование расхода/кликов/показов из CSV рекламы.
        Ищет колонки по подстрокам, устойчиво к вариациям названий.
        """
        def num(x):
            if x is None:
                return 0.0
            x = str(x).replace("\xa0", "").replace(" ", "").replace(",", ".")
            try:
                return float(x)
            except ValueError:
                return 0.0

        totals = {"spend": 0.0, "clicks": 0.0, "views": 0.0}
        for row in rows:
            for k, v in row.items():
                if not k:
                    continue
                kl = k.lower()
                if "расход" in kl or "spend" in kl or "cost" in kl:
                    totals["spend"] += num(v)
                elif "клик" in kl or "click" in kl:
                    totals["clicks"] += num(v)
                elif "показ" in kl or "view" in kl or "impression" in kl:
                    totals["views"] += num(v)
        return totals
