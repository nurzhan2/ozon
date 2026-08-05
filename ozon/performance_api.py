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
import requests

log = logging.getLogger("ozon.perf")

BASE_URL = "https://api-performance.ozon.ru"

# OZON принимает не больше 10 кампаний в одном запросе статистики
# ("Превышен лимит по количеству кампаний (максимум 10)").
MAX_CAMPAIGNS_PER_REQUEST = 10

# Верхняя граница на число кампаний в одном отчёте. Статистика асинхронная
# (запрос -> ожидание -> CSV), поэтому 289 кампаний = 29 отчётов и десятки
# минут ожидания. Ограничение делает сбор предсказуемым по времени; сколько
# кампаний отброшено, всегда пишется в лог — тихих усечений быть не должно.
MAX_CAMPAIGNS_TOTAL = int(os.environ.get("PERF_MAX_CAMPAIGNS", "60"))


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

    def _auth(self):
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

    def _get(self, path, **kw):
        self._auth()
        r = self.session.get(BASE_URL + path, timeout=self.timeout, **kw)
        if r.status_code != 200:
            raise PerformanceAPIError(f"[{self.name}] GET {path} HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _post(self, path, payload):
        self._auth()
        r = self.session.post(BASE_URL + path, json=payload, timeout=self.timeout)
        if r.status_code not in (200, 202):
            raise PerformanceAPIError(f"[{self.name}] POST {path} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

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

        if len(campaign_ids) > MAX_CAMPAIGNS_TOTAL:
            log.warning("[%s] кампаний %d, в отчёт войдут первые %d "
                        "(ограничение PERF_MAX_CAMPAIGNS) — остальные пропущены",
                        self.name, len(campaign_ids), MAX_CAMPAIGNS_TOTAL)
            campaign_ids = campaign_ids[:MAX_CAMPAIGNS_TOTAL]

        # OZON не принимает больше 10 кампаний за раз — идём пачками
        if len(campaign_ids) > MAX_CAMPAIGNS_PER_REQUEST:
            rows = []
            for i in range(0, len(campaign_ids), MAX_CAMPAIGNS_PER_REQUEST):
                chunk = campaign_ids[i:i + MAX_CAMPAIGNS_PER_REQUEST]
                try:
                    rows.extend(self.statistics(date_from, date_to, chunk, group_by))
                except PerformanceAPIError as e:
                    log.warning("[%s] пачка кампаний %d-%d пропущена: %s",
                                self.name, i + 1, i + len(chunk), e)
            return rows

        payload = {
            "campaigns": [str(c) for c in campaign_ids],
            "from": f"{date_from}T00:00:00.000Z",
            "to": f"{date_to}T23:59:59.000Z",
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        }
        resp = self._post("/api/client/statistics", payload)
        uuid = resp.get("UUID") or resp.get("uuid")
        if not uuid:
            raise PerformanceAPIError(f"[{self.name}] statistics: не получен UUID: {resp}")

        # ожидание готовности
        for _ in range(60):
            st = self._get(f"/api/client/statistics/{uuid}").json()
            state = (st.get("state") or st.get("status") or "").upper()
            if state in ("OK", "SUCCESS", "DONE"):
                break
            if state in ("ERROR", "FAILED"):
                raise PerformanceAPIError(f"[{self.name}] отчёт рекламы завершился с ошибкой: {st}")
            time.sleep(3)
        else:
            raise PerformanceAPIError(f"[{self.name}] отчёт рекламы не готов за отведённое время")

        # скачивание CSV
        r = self._get("/api/client/statistics/report", params={"UUID": uuid})
        text = r.content.decode("utf-8-sig", errors="replace")
        # OZON отдаёт CSV с разделителем ';'
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        return [row for row in reader]

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
        totals = {"spend": 0.0, "clicks": 0.0, "views": 0.0}
        for row in rows:
            for k, v in row.items():
                if not k:
                    continue
                kl = k.lower()
                if "расход" in kl or "spend" in kl or "cost" in kl:
                    totals["spend"] += _num(v)
                elif "клик" in kl or "click" in kl:
                    totals["clicks"] += _num(v)
                elif "показ" in kl or "view" in kl or "impression" in kl:
                    totals["views"] += _num(v)
        return totals
