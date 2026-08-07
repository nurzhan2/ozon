# -*- coding: utf-8 -*-
"""
Сбор данных по одному магазину: справочник товаров, остатки (в т.ч. по кластерам),
подневная аналитика, расход рекламы по товарам.
Справочники кэшируются в рамках одного запуска.
"""

import logging

from .seller_api import SellerAPI, SellerAPIError, METRICS_REPORT
from .performance_api import PerformanceAPI
from . import processing as P
from . import dates as D

log = logging.getLogger("ozon.collector")


def _slice_days(data, date_from, date_to):
    """Вырезает из {товар: {день: расход}} только дни внутри периода."""
    out = {}
    for key, days in (data or {}).items():
        sub = {d: v for d, v in days.items() if date_from <= d <= date_to}
        if sub:
            out[key] = sub
    return out


class StoreCollector:
    def __init__(self, store_cfg, enable_performance=True, exclude_marker="OUT"):
        self.cfg = store_cfg
        self.name = store_cfg["name"]
        self.exclude_marker = exclude_marker
        self.enable_performance = enable_performance
        self.seller = SellerAPI(store_cfg["client_id"], store_cfg["api_key"], name=self.name)
        self.perf = None
        if enable_performance and store_cfg.get("perf_client_id"):
            self.perf = PerformanceAPI(
                store_cfg["perf_client_id"], store_cfg["perf_client_secret"], name=self.name
            )
        self._sku_map = None
        self._offer_map = None
        self._stocks = None
        self._cluster_rows = None
        # Один собранный период рекламы на весь запуск. Отчёты просят разные
        # отрезки (месяц с начала, вчера, позавчера), но все они лежат внутри
        # самого широкого — его и режем локально, вместо новых походов в API.
        # Раньше каждый отрезок стоил полного прохода по всем кампаниям, и
        # утренний пакет тратил три прохода вместо одного.
        self._ad_data = {}
        self._ad_range = None       # ('YYYY-MM-DD', 'YYYY-MM-DD') или None
        self._ad_dated = True
        # Подневная аналитика по периодам. /v1/analytics/data разрешён не чаще
        # раза в минуту, а отчёты 1 и 3 просят один и тот же период — без кэша
        # это лишний дорогой запрос на каждый магазин.
        self._daily_cache = {}

    # ---------------- справочники ----------------
    def maps(self):
        if self._sku_map is None:
            log.info("[%s] загрузка справочника товаров...", self.name)
            items = self.seller.product_list()
            pids = [it["product_id"] for it in items if it.get("product_id")]
            self._sku_map, self._offer_map = self.seller.product_info_list(pids)
            log.info("[%s] товаров: %d, sku в маппинге: %d",
                     self.name, len(pids), len(self._sku_map))
        return self._sku_map, self._offer_map

    def stocks(self):
        if self._stocks is None:
            log.info("[%s] загрузка остатков...", self.name)
            self._stocks = self.seller.stocks()
        return self._stocks

    def offer_names(self):
        _, offer_map = self.maps()
        return {v["offer_id"]: v["name"] for v in offer_map.values() if v.get("offer_id")}

    def in_stock_offers(self):
        """
        Множество артикулов, у которых есть остаток (>0) и нет метки OUT.

        Если остатки не пришли вовсе, возвращает None — «не фильтровать».
        Пустое множество здесь означало бы, что на остатках нет ничего, и
        отчёты 1-3 выходили бы с одними шапками при живой аналитике. Ровно так
        и случилось, когда OZON поменял формат ответа по остаткам: молчаливая
        пустота вместо ошибки. Лучше показать продажи без фильтра по остаткам
        и громко предупредить, чем отдать пустой отчёт.
        """
        stocks = self.stocks()
        if not stocks:
            log.warning("[%s] остатков нет ни по одному товару — фильтр «только "
                        "на остатках» отключён, в отчёт войдут все товары "
                        "с продажами", self.name)
            return None
        return {
            offer_id for offer_id, st in stocks.items()
            if st.get("present", 0) > 0 and not P.is_excluded(offer_id, self.exclude_marker)
        }

    # ---------------- остатки по кластерам ----------------
    def cluster_stocks(self):
        """
        Строки «артикул × кластер» для отчёта 4.
        Пробует кластерный метод, при недоступности откатывается на склады.
        """
        if self._cluster_rows is None:
            # Кластерному методу нужен список sku магазина: он принимает их
            # пачками до 100, а не limit/offset. Справочник уже загружен.
            sku_map, _ = self.maps()
            try:
                log.info("[%s] загрузка остатков по кластерам (sku: %d)...",
                         self.name, len(sku_map))
                rows = self.seller.cluster_stocks(skus=list(sku_map))
                log.info("[%s] кластерных строк: %d", self.name, len(rows))
            except SellerAPIError as e:
                log.warning("[%s] кластерный метод недоступен (%s), беру склады", self.name, e)
                rows = self.seller.stocks_on_warehouses()
            names = self.offer_names()
            for r in rows:
                if not r.get("name"):
                    r["name"] = names.get(r.get("offer_id", ""), "")
            self._cluster_rows = [
                r for r in rows
                if not P.is_excluded(r.get("offer_id", ""), self.exclude_marker)
            ]
        return self._cluster_rows

    # ---------------- аналитика ----------------
    def daily_by_product(self, date_from, date_to, only_in_stock=True):
        """
        Подневная аналитика по товарам.
        Возвращает: {offer_id: {"name":..., "days": {"YYYY-MM-DD": {метрики}}}}
        """
        df = D.d(date_from) if hasattr(date_from, "strftime") else date_from
        dt = D.d(date_to) if hasattr(date_to, "strftime") else date_to
        key = (df, dt, bool(only_in_stock))
        if key in self._daily_cache:
            return self._daily_cache[key]

        sku_map, _ = self.maps()
        rows, order = self.seller.analytics_data(df, dt, dimension=("sku", "day"),
                                                 metrics=METRICS_REPORT)
        allowed = self.in_stock_offers() if only_in_stock else None
        result = P.rows_to_daily(rows, sku_map, order, allowed, self.exclude_marker)
        # подмешиваем расход рекламы
        ads = self.ad_spend(df, dt)
        if ads:
            P.merge_ad_spend(result, ads, sku_map)
        self._daily_cache[key] = (result, order)
        return result, order

    def products_for_period(self, date_from, date_to, only_in_stock=True,
                            metrics=None, with_kpi=True):
        """Суммарная аналитика по товарам за период (используется отчётом 1 и остатками)."""
        metrics = metrics or METRICS_REPORT
        df = D.d(date_from) if hasattr(date_from, "strftime") else date_from
        dt = D.d(date_to) if hasattr(date_to, "strftime") else date_to
        sku_map, _ = self.maps()
        stocks = self.stocks()
        rows, order = self.seller.analytics_data(df, dt, dimension=("sku",), metrics=metrics)
        products = P.rows_to_products(rows, sku_map, order)
        products = P.filter_products(products, stocks, self.exclude_marker, only_in_stock)
        if with_kpi:
            for rec in products.values():
                P.add_kpis(rec)
        return products

    # ---------------- реклама ----------------
    def ad_spend(self, date_from, date_to):
        """
        {ключ_товара: {день: расход}} за запрошенный период.

        Ходит в Performance API не больше одного раза за запуск: отчёт идёт с
        группировкой по дням, поэтому уже собранный широкий период режется по
        датам локально. Стоимость похода — примерно сотня запросов из суточных
        двух тысяч на аккаунт, так что экономия здесь принципиальная, а не
        косметическая.
        """
        if not self.perf:
            return {}
        df = D.d(date_from) if hasattr(date_from, "strftime") else str(date_from)
        dt = D.d(date_to) if hasattr(date_to, "strftime") else str(date_to)

        have = self._ad_range
        if have and have[0] <= df and dt <= have[1]:
            if not self._ad_dated and (df, dt) != have:
                # В отчёте не было колонки с датой: резать нечего и нельзя.
                log.warning("[%s] расход рекламы без разбивки по дням — "
                            "период %s..%s взят целиком", self.name, *have)
                return self._ad_data
            return _slice_days(self._ad_data, df, dt)

        # Расширяемся до объединения с уже собранным, чтобы второй поход
        # закрыл сразу всё, что может понадобиться дальше.
        need_from = min(df, have[0]) if have else df
        need_to = max(dt, have[1]) if have else dt
        try:
            self._ad_data = self.perf.spend_by_product_day(need_from, need_to)
            self._ad_dated = getattr(self.perf, "last_spend_dated", True)
            self._ad_range = (need_from, need_to)
        except Exception as e:
            log.warning("[%s] реклама недоступна: %s", self.name, e)
            # Запоминаем неудачу как закрытый период: повторять поход в тот же
            # API в рамках одного запуска бессмысленно и дорого по лимиту.
            if not have:
                self._ad_data, self._ad_dated = {}, True
                self._ad_range = (need_from, need_to)
            return _slice_days(self._ad_data, df, dt) if self._ad_dated else self._ad_data

        return _slice_days(self._ad_data, df, dt) if self._ad_dated else self._ad_data

    def performance_totals(self, date_from, date_to):
        if not self.perf:
            return None
        try:
            rows = self.perf.statistics(
                D.d(date_from) if hasattr(date_from, "strftime") else date_from,
                D.d(date_to) if hasattr(date_to, "strftime") else date_to,
            )
            return self.perf.aggregate_totals(rows)
        except Exception as e:
            log.warning("[%s] реклама недоступна: %s", self.name, e)
            return None
