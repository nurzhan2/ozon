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

Реализован сбор агрегированных показателей рекламы за период по кампаниям.
Если реклама не нужна — модуль можно не использовать (ENABLE_PERFORMANCE=False).
"""

import io
import os
import csv
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger("ozon.perf")

BASE_URL = "https://api-performance.ozon.ru"

# OZON принимает не больше 10 кампаний в одном запросе статистики
# ("Превышен лимит по количеству кампаний (максимум 10)").
MAX_CAMPAIGNS_PER_REQUEST = 10

# Верхняя граница на число кампаний в отчёте. 0 — без ограничения, берутся ВСЕ.
# По умолчанию именно 0: усечение молча искажает «рекламу» и «ДРР», а это те
# самые колонки, ради которых отчёт и собирается. Переменная оставлена как
# аварийный тормоз, если у магазина окажутся сотни кампаний и сбор перестанет
# укладываться в окно до 8 утра.
MAX_CAMPAIGNS_TOTAL = int(os.environ.get("PERF_MAX_CAMPAIGNS", "0"))

# Сколько пачек собирать одновременно.
#
# ПО УМОЛЧАНИЮ 1, И МЕНЯТЬ ЭТО НЕ НАДО. Performance API разрешает ровно один
# активный запрос статистики на аккаунт: при попытке запустить второй он
# отвечает 429 «Превышен лимит активных запросов (максимум 1)». Проверено на
# боевом аккаунте — при четырёх потоках отвалились 4 пачки из 12.
#
# Параметр оставлен на случай, если OZON когда-нибудь поднимет лимит. Ставить
# больше 1 сейчас — гарантированно терять часть расхода на рекламу.
PARALLEL_BATCHES = max(1, int(os.environ.get("PERF_PARALLEL", "1")))

# Сколько ждать готовности одного отчёта и как часто спрашивать статус.
REPORT_TIMEOUT = float(os.environ.get("PERF_REPORT_TIMEOUT", "300"))
POLL_INTERVAL = float(os.environ.get("PERF_POLL_INTERVAL", "3"))

# Как часто напоминать в лог, что отчёт всё ещё готовится.
POLL_NOTICE = float(os.environ.get("PERF_POLL_NOTICE", "30"))


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


class PerformanceAPIError(Exception):
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
        # поэтому обёрнуто замком: иначе два потока разом полезут за токеном и
        # один затрёт заголовок другого на полуслове.
        self._auth_lock = threading.Lock()

    def _auth(self):
        with self._auth_lock:
            self._auth_locked()

    def _auth_locked(self):
        if self._token and time.time() < self._token_exp - 60:
            return
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

    # Временные отказы повторяются с нарастающей паузой: терять из-за них целую
    # пачку кампаний (а с ней часть расхода на рекламу) незачем.
    RETRY_CODES = (429, 500, 502, 503, 504)
    RETRIES = 6

    def _request(self, method, path, ok_codes, **kw):
        last = None
        for attempt in range(1, self.RETRIES + 1):
            self._auth()
            r = self.session.request(method, BASE_URL + path,
                                     timeout=self.timeout, **kw)
            if r.status_code in ok_codes:
                return r
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in self.RETRY_CODES or attempt == self.RETRIES:
                break

            # «Превышен лимит активных запросов» означает, что предыдущий отчёт
            # ещё готовится. Частым опросом делу не поможешь — нужно ждать,
            # пока освободится единственный слот, поэтому пауза длиннее.
            busy = "активных запросов" in r.text
            pause = min(10 * attempt, 60) if busy else min(3 * attempt, 15)
            log.debug("[%s] %s %s -> %d, повтор через %.0f с",
                      self.name, method, path, r.status_code, pause)
            time.sleep(pause)
        raise PerformanceAPIError(f"[{self.name}] {method} {path} {last}")

    def _get(self, path, **kw):
        return self._request("GET", path, (200,), **kw)

    def _post(self, path, payload):
        return self._request("POST", path, (200, 202), json=payload).json()

    def campaigns(self, only_active=True):
        """
        GET /api/client/campaign -> список кампаний.
        Сначала идут активные: если кампаний больше лимита, в отчёт попадут
        именно работающие, а не архивные.
        """
        data = self._get("/api/client/campaign").json()
        items = data.get("list") or data.get("campaigns") or []
        if not only_active:
            return items

        def is_active(c):
            state = str(c.get("state") or c.get("status") or "").upper()
            return "RUNNING" in state or "ACTIVE" in state

        active = [c for c in items if is_active(c)]
        return active or items          # если статусы не распознались — берём все

    def statistics(self, date_from, date_to, campaign_ids=None, group_by="DATE"):
        """
        Запрашивает статистику и дожидается готовности отчёта.
        Возвращает список dict-строк CSV (ключи — заголовки OZON, могут
        отличаться по локали: 'Расход, руб.', 'Клики', 'Показы', и т.п.).
        date_from/date_to: 'YYYY-MM-DD'.
        """
        if campaign_ids is None:
            campaign_ids = [str(c.get("id")) for c in self.campaigns() if c.get("id")]
        if not campaign_ids:
            return []

        if MAX_CAMPAIGNS_TOTAL and len(campaign_ids) > MAX_CAMPAIGNS_TOTAL:
            log.warning("[%s] кампаний %d, в отчёт войдут первые %d "
                        "(ограничение PERF_MAX_CAMPAIGNS) — остальные пропущены",
                        self.name, len(campaign_ids), MAX_CAMPAIGNS_TOTAL)
            campaign_ids = campaign_ids[:MAX_CAMPAIGNS_TOTAL]

        # OZON не принимает больше 10 кампаний за раз — идём пачками, по одной
        if len(campaign_ids) > MAX_CAMPAIGNS_PER_REQUEST:
            chunks = [campaign_ids[i:i + MAX_CAMPAIGNS_PER_REQUEST]
                      for i in range(0, len(campaign_ids), MAX_CAMPAIGNS_PER_REQUEST)]
            workers = min(PARALLEL_BATCHES, len(chunks))
            log.info("[%s] кампаний %d -> %d пачек по %d%s",
                     self.name, len(campaign_ids), len(chunks),
                     MAX_CAMPAIGNS_PER_REQUEST,
                     "" if workers == 1 else f", в {workers} потока(ов)")

            started = time.monotonic()
            rows, failed, done = [], 0, 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                tasks = {
                    pool.submit(self._statistics_batch, date_from, date_to,
                                chunk, group_by,
                                f"{n}/{len(chunks)}"): (n, chunk)
                    for n, chunk in enumerate(chunks, 1)
                }
                for task in as_completed(tasks):
                    n, chunk = tasks[task]
                    done += 1
                    try:
                        rows.extend(task.result())
                    # Ловим любое исключение, а не только своё: сломанный CSV в
                    # одной пачке не повод потерять рекламу по всему магазину.
                    except Exception as e:
                        failed += 1
                        log.warning("[%s] пачка %d/%d (кампании %s) пропущена: %s: %s",
                                    self.name, n, len(chunks), ",".join(chunk),
                                    type(e).__name__, e)

            if failed:
                log.warning("[%s] не собрано пачек: %d из %d — "
                            "«реклама» и «ДРР» по этим кампаниям будут занижены",
                            self.name, failed, len(chunks))
            log.info("[%s] статистика рекламы собрана за %.0f с, строк: %d",
                     self.name, time.monotonic() - started, len(rows))
            return rows

        return self._statistics_batch(date_from, date_to, campaign_ids, group_by)

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
        resp = self._post("/api/client/statistics", payload)
        uuid = resp.get("UUID") or resp.get("uuid")
        if not uuid:
            raise PerformanceAPIError(f"[{self.name}] statistics: не получен UUID: {resp}")

        # Ожидание готовности. Ограничение по времени, а не по числу опросов:
        # когда пачек много, OZON ставит отчёты в очередь и ждать приходится
        # дольше, чем при одиночном запросе.
        #
        # Раз в POLL_NOTICE секунд пишем, что всё ещё ждём: отчёт может
        # готовиться минуту и дольше, и без отметки процесс выглядит зависшим.
        started = time.monotonic()
        deadline = started + REPORT_TIMEOUT
        next_notice = started + POLL_NOTICE
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
            time.sleep(POLL_INTERVAL)
        else:
            raise PerformanceAPIError(
                f"[{self.name}] отчёт рекламы не готов за {REPORT_TIMEOUT:.0f} с "
                f"(кампании {','.join(str(c) for c in campaign_ids)})"
            )

        # скачивание CSV
        r = self._get("/api/client/statistics/report", params={"UUID": uuid})
        text = r.content.decode("utf-8-sig", errors="replace")

        # OZON отдаёт CSV с разделителем ';' и переносами '\r\n'.
        #
        # newline="" здесь обязателен. Без него StringIO режет текст только по
        # '\n', в конце каждой строки остаётся '\r', и csv падает с «new-line
        # character seen in unquoted field». Именно так весь сбор рекламы и
        # обвалился на боевом прогоне. С newline="" разбор переносов остаётся
        # за csv — заодно переживают и многострочные значения в кавычках.
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";")
        out = [row for row in reader]
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
            day = _norm_date(row.get(k_date)) if k_date else str(date_from)
            out.setdefault(sku, {})
            out[sku][day] = out[sku].get(day, 0.0) + _num(row.get(k_spend))
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
