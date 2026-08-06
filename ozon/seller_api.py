# -*- coding: utf-8 -*-
"""
Клиент OZON Seller API (аналитика, товары, остатки).

Документация: https://docs.ozon.ru/api/seller/
Базовый URL:  https://api-seller.ozon.ru
Авторизация:  заголовки Client-Id (числовой) + Api-Key.

ВНИМАНИЕ: OZON периодически меняет версии методов и названия полей.
Если какой-то запрос вернёт ошибку про неизвестное поле/метод — сверьтесь
с актуальной документацией и поправьте имена в этом файле (они вынесены
в константы вверху методов).
"""

import os
import time
import logging
import threading

import requests

log = logging.getLogger("ozon.seller")

BASE_URL = "https://api-seller.ozon.ru"

# Полный набор качественных метрик аналитики.
# OZON ограничивает число метрик в одном запросе (обычно <= 14),
# поэтому список автоматически разбивается на части и склеивается.
ANALYTICS_METRICS = [
    "revenue",              # выручка, руб
    "ordered_units",        # заказано товаров, шт
    "delivered_units",      # доставлено, шт
    "returns",              # возвраты, шт
    "cancellations",        # отмены, шт
    "hits_view_search",     # показы в поиске и категориях
    "hits_view_pdp",        # показы в карточке товара
    "hits_view",            # показы всего
    "hits_tocart_search",   # в корзину из поиска/категорий
    "hits_tocart_pdp",      # в корзину из карточки
    "hits_tocart",          # в корзину всего
    "session_view_search",  # сессии с показом в поиске/категориях
    "session_view_pdp",     # сессии с показом в карточке
    "session_view",         # сессии с показом всего
    "conv_tocart_search",   # конверсия в корзину из поиска
    "conv_tocart_pdp",      # конверсия в корзину из карточки
    "conv_tocart",          # конверсия в корзину всего
    "position_category",    # средняя позиция в поиске и категории
]

# Метрики, которые нужны отчётам (сокращённый набор — быстрее и меньше лимитов).
METRICS_REPORT = [
    "revenue",              # оборот / сумма
    "ordered_units",        # заказано, шт
    "cancellations",        # отмены
    "hits_view",            # показы
    "session_view",         # клики (переходы в карточку)
    "hits_tocart",          # добавления в корзину
    "position_category",    # место в поиске
]

METRICS_PER_REQUEST = 14


# ----------------------------------------------------------------------------
# Ограничитель скорости.
# OZON держит жёсткий лимит Seller API: 2 запроса в секунду на клиента
# ("rate limit exceeded for `seller-api` client, current max rate per sec.: 2").
# Лимит общий на процесс, поэтому ограничитель — на уровне модуля, а не
# экземпляра: иначе пять магазинов подряд легко выбивают 429.
# ----------------------------------------------------------------------------
_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MIN_INTERVAL = float(os.environ.get("OZON_MIN_INTERVAL", "0.6"))   # сек между запросами


# Отдельные методы аналитики держат лимит куда жёстче общих двух запросов
# в секунду и отвечают 429 «You have reached request rate limit per second»
# даже на одиночный вызов, если предыдущий был недавно. На боевом прогоне так
# отвалились кластерные остатки «Штучки»: четыре попытки с паузами 5-20 с
# закончились ничем, и отчёт 4 по этому магазину собрался по складам —
# без «в поставках в пути» и среднесуточных продаж.
#
# Для таких адресов держим свой, более редкий шаг и более длинные паузы
# при отказе.
SLOW_ENDPOINTS = {
    "/v1/analytics/stocks": float(os.environ.get("OZON_STOCKS_INTERVAL", "4")),
}
_LAST_SLOW_CALL = {}


def _throttle(path=None):
    """Выдерживает паузу так, чтобы не превышать разрешённую частоту."""
    with _RATE_LOCK:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _LAST_CALL[0])
        slow = SLOW_ENDPOINTS.get(path)
        if slow:
            since = now - _LAST_SLOW_CALL.get(path, 0.0)
            wait = max(wait, slow - since)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()
        if slow:
            _LAST_SLOW_CALL[path] = _LAST_CALL[0]


class SellerAPIError(Exception):
    """Ошибка Seller API. network=True — до OZON не достучались (сеть/прокси)."""

    def __init__(self, message, network=False, status=None):
        super().__init__(message)
        self.network = network
        self.status = status


class SellerAPI:
    def __init__(self, client_id, api_key, name="", max_retries=4, timeout=60):
        self.client_id = str(client_id)
        self.api_key = api_key
        self.name = name
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        })

    # ---- низкоуровневый POST с ретраями и обработкой лимитов ----
    def _post(self, path, payload):
        url = BASE_URL + path
        last_err = None
        network_only = True   # ни одного ответа от OZON так и не получили
        tries = self.max_retries + (3 if path in SLOW_ENDPOINTS else 0)
        for attempt in range(1, tries + 1):
            _throttle(path)
            try:
                r = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("[%s] %s сетевая ошибка (%s), попытка %d/%d",
                            self.name, path, e, attempt, tries)
                if attempt < tries:
                    time.sleep(min(2 ** attempt, 15))
                continue

            network_only = False
            if r.status_code == 200:
                return r.json()

            # 429 / 5xx — повторяем с паузой
            if r.status_code in (429, 500, 502, 503, 504):
                # 429 — упёрлись в лимит частоты: ждём заметно дольше
                if r.status_code == 429:
                    # У «медленных» адресов лимит жёстче: паузы длиннее,
                    # иначе попытки кончаются раньше, чем OZON нас отпускает.
                    wait = (min(15 * attempt, 90) if path in SLOW_ENDPOINTS
                            else min(5 * attempt, 30))
                else:
                    wait = min(2 ** attempt, 30)
                log.warning("[%s] %s HTTP %d, пауза %ds (попытка %d/%d)",
                            self.name, path, r.status_code, wait, attempt, tries)
                last_err = SellerAPIError(f"HTTP {r.status_code}: {r.text[:300]}",
                                          status=r.status_code)
                if attempt < tries:
                    time.sleep(wait)
                continue

            # 401/403 — почти всегда неверный client_id/api_key
            if r.status_code in (401, 403):
                raise SellerAPIError(
                    f"[{self.name}] Авторизация не прошла (HTTP {r.status_code}). "
                    f"Проверьте client_id и api_key. Ответ: {r.text[:300]}",
                    status=r.status_code,
                )

            raise SellerAPIError(f"[{self.name}] {path} HTTP {r.status_code}: {r.text[:500]}",
                                 status=r.status_code)

        if network_only:
            raise SellerAPIError(
                f"[{self.name}] Нет соединения с api-seller.ozon.ru. "
                f"Причина: {last_err}",
                network=True,
            )
        raise SellerAPIError(f"[{self.name}] {path}: исчерпаны попытки. {last_err}",
                             status=getattr(last_err, "status", None))

    # ---------------- Аналитика ----------------
    def analytics_data(self, date_from, date_to, dimension=("sku", "day"),
                       metrics=None, limit=1000):
        """
        POST /v1/analytics/data
        Возвращает список строк вида:
          {"dimensions":[{"id":"<sku>","name":"<название>"}, {"id":"2026-07-01"}],
           "metrics":[<по порядку из metrics>]}
        Метрики автоматически разбиваются на группы и склеиваются по ключу
        (id измерений). date_from/date_to — строки 'YYYY-MM-DD'.
        """
        metrics = list(metrics or ANALYTICS_METRICS)
        chunks = [metrics[i:i + METRICS_PER_REQUEST]
                  for i in range(0, len(metrics), METRICS_PER_REQUEST)]

        merged = {}   # ключ измерений -> {"dimensions":..., metric_name: value}
        metric_order = []

        for chunk in chunks:
            for name in chunk:
                if name not in metric_order:
                    metric_order.append(name)
            offset = 0
            while True:
                payload = {
                    "date_from": date_from,
                    "date_to": date_to,
                    "metrics": chunk,
                    "dimension": list(dimension),
                    "filters": [],
                    "sort": [{"key": chunk[0], "order": "DESC"}],
                    "limit": limit,
                    "offset": offset,
                }
                data = self._post("/v1/analytics/data", payload)
                rows = (data.get("result") or {}).get("data") or []
                for row in rows:
                    dims = row.get("dimensions", [])
                    key = tuple(d.get("id") for d in dims)
                    rec = merged.setdefault(key, {"dimensions": dims})
                    for name, val in zip(chunk, row.get("metrics", [])):
                        rec[name] = val
                if len(rows) < limit:
                    break
                offset += limit
        # приводим к списку
        result = []
        for rec in merged.values():
            for name in metric_order:
                rec.setdefault(name, 0)
            result.append(rec)
        return result, metric_order

    # ---------------- Товары ----------------
    def product_list(self, limit=1000):
        """POST /v3/product/list -> [{product_id, offer_id, ...}]"""
        items = []
        last_id = ""
        while True:
            payload = {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": limit}
            data = self._post("/v3/product/list", payload)
            res = data.get("result") or {}
            batch = res.get("items") or []
            items.extend(batch)
            last_id = res.get("last_id") or ""
            if not batch or not last_id or len(batch) < limit:
                break
        return items

    def product_info_list(self, product_ids):
        """
        POST /v3/product/info/list -> подробности по товарам.
        Возвращает dict: sku(int) -> {"offer_id":..., "name":..., "product_id":...}
        Собирает все возможные sku (fbo/fbs/основной) для маппинга аналитики.
        """
        sku_map = {}
        offer_map = {}   # product_id -> {offer_id, name}
        CH = 900
        for i in range(0, len(product_ids), CH):
            chunk = product_ids[i:i + CH]
            payload = {"product_id": chunk, "offer_id": [], "sku": []}
            data = self._post("/v3/product/info/list", payload)
            items = (data.get("result") or {}).get("items")
            if items is None:
                items = data.get("items") or []
            for it in items:
                pid = it.get("id") or it.get("product_id")
                offer_id = it.get("offer_id", "")
                name = it.get("name", "")
                offer_map[pid] = {"offer_id": offer_id, "name": name, "product_id": pid}
                # собираем все sku из разных полей
                skus = set()
                for k in ("sku", "fbo_sku", "fbs_sku"):
                    v = it.get(k)
                    if v:
                        skus.add(int(v))
                for src in (it.get("sources") or []):
                    v = src.get("sku")
                    if v:
                        skus.add(int(v))
                for s in skus:
                    sku_map[s] = {"offer_id": offer_id, "name": name, "product_id": pid}
        return sku_map, offer_map

    # ---------------- Остатки ----------------
    def stocks(self, limit=1000):
        """
        POST /v4/product/info/stocks -> остатки по товарам.
        Возвращает dict: offer_id -> {"present": int, "reserved": int, "product_id": id}
        present — доступно к продаже (сумма по складам), reserved — зарезервировано.
        """
        result = {}
        last_id = ""
        while True:
            payload = {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": limit}
            data = self._post("/v4/product/info/stocks", payload)
            res = data.get("result") or {}
            items = res.get("items") or []
            for it in items:
                offer_id = it.get("offer_id", "")
                present = reserved = 0
                for st in (it.get("stocks") or []):
                    present += int(st.get("present", 0) or 0)
                    reserved += int(st.get("reserved", 0) or 0)
                result[offer_id] = {
                    "present": present,
                    "reserved": reserved,
                    "product_id": it.get("product_id"),
                }
            last_id = res.get("last_id") or ""
            if not items or not last_id or len(items) < limit:
                break
        return result

    # ---------------- Остатки по кластерам ----------------
    # /v1/analytics/stocks принимает СПИСОК SKU, а не limit/offset, и требует
    # от 1 до 100 штук за раз. Проба пустым фильтром отбивалась с 400
    # «invalid AnalyticsStocksRequest.Skus: value must contain between 1 and
    # 100 items», метод считался недоступным у всех пяти магазинов, и отчёт 4
    # молча собирался по складам. А по складам нет ни «в пути», ни
    # среднесуточных продаж OZON — две колонки образца оставались нулями.
    STOCKS_SKU_CHUNK = 100

    @staticmethod
    def _cluster_row(it):
        return {
            "offer_id": it.get("offer_id", ""),
            "sku": it.get("sku"),
            "name": it.get("name", "") or it.get("title", ""),
            "cluster": it.get("cluster_name") or it.get("cluster") or "",
            "warehouse": it.get("warehouse_name", ""),
            "available": _i(it.get("available_stock_count") or it.get("valid_stock_count")),
            "requested": _i(it.get("requested_stock_count")),
            "transit": _i(it.get("transit_stock_count")),
            "ads": _f(it.get("ads")),
            "idc": _f(it.get("idc")),
        }

    def _cluster_stocks_by_sku(self, skus):
        """/v1/analytics/stocks — пачками не больше 100 sku за запрос."""
        uniq = list(dict.fromkeys(str(x) for x in skus if x))
        if not uniq:
            raise SellerAPIError(f"[{self.name}] нет sku для кластерных остатков")
        rows = []
        for i in range(0, len(uniq), self.STOCKS_SKU_CHUNK):
            chunk = uniq[i:i + self.STOCKS_SKU_CHUNK]
            data = self._post("/v1/analytics/stocks", {"skus": chunk})
            items = data.get("items") or (data.get("result") or {}).get("items") or []
            rows.extend(self._cluster_row(it) for it in items)
        return rows

    def cluster_stocks(self, skus=None, limit=1000):
        """
        Остатки в разрезе КЛАСТЕРОВ — дают колонки образца:
          «Доступно к продаже», «В заявках на поставку», «В поставках в пути».

        Возвращает список строк:
          {"offer_id","name","cluster","available","requested","transit",
           "ads" (среднесуточные продажи по данным OZON), "idc" (дней хватит)}

        skus — список sku магазина. Без него остаётся только старый путь
        с пагинацией. Если не отвечает ни один — поднимает SellerAPIError,
        и вызывающий код откатится на склады.
        """
        errors = []
        if skus:
            try:
                return self._cluster_stocks_by_sku(skus)
            except SellerAPIError as e:
                errors.append(str(e))
                log.debug("[%s] /v1/analytics/stocks не отдал кластеры: %s",
                          self.name, e)

        # Старый путь: OZON менял адрес метода, у части аккаунтов работает
        # вариант с limit/offset.
        try:
            self._post("/v1/analytics/manage/stocks",
                       {"limit": 1, "offset": 0, "filter": {}})
        except SellerAPIError as e:
            errors.append(str(e))
            raise SellerAPIError(f"[{self.name}] кластерные остатки недоступны: "
                                 + "; ".join(errors[-2:]))

        rows = []
        offset = 0
        while True:
            payload = {"limit": limit, "offset": offset, "filter": {}}
            data = self._post("/v1/analytics/manage/stocks", payload)
            items = data.get("items") or (data.get("result") or {}).get("items") or []
            rows.extend(self._cluster_row(it) for it in items)
            if len(items) < limit:
                break
            offset += limit
        return rows

    # ---------------- Остатки по складам (запасной вариант) ----------------
    def stocks_on_warehouses(self, limit=1000):
        """
        POST /v2/analytics/stock_on_warehouses — остатки по складам.
        Используется, если кластерный метод недоступен.
        """
        rows = []
        offset = 0
        while True:
            payload = {"limit": limit, "offset": offset, "warehouse_type": "ALL"}
            data = self._post("/v2/analytics/stock_on_warehouses", payload)
            items = (data.get("result") or {}).get("rows") or []
            for it in items:
                rows.append({
                    "offer_id": it.get("item_code", ""),
                    "sku": it.get("sku"),
                    "name": it.get("item_name", ""),
                    "cluster": it.get("warehouse_name", ""),
                    "warehouse": it.get("warehouse_name", ""),
                    "available": _i(it.get("free_to_sell_amount")),
                    "requested": _i(it.get("promised_amount")),
                    "transit": 0,
                    "ads": 0.0,
                    "idc": 0.0,
                })
            if len(items) < limit:
                break
            offset += limit
        return rows


def _i(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
