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
        self._ad_cache = {}

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
        """Множество артикулов, у которых есть остаток (>0) и нет метки OUT."""
        return {
            offer_id for offer_id, st in self.stocks().items()
            if st.get("present", 0) > 0 and not P.is_excluded(offer_id, self.exclude_marker)
        }

    # ---------------- остатки по кластерам ----------------
    def cluster_stocks(self):
        """
        Строки «артикул × кластер» для отчёта 4.
        Пробует кластерный метод, при недоступности откатывается на склады.
        """
        if self._cluster_rows is None:
            try:
                log.info("[%s] загрузка остатков по кластерам...", self.name)
                rows = self.seller.cluster_stocks()
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
        sku_map, _ = self.maps()
        rows, order = self.seller.analytics_data(df, dt, dimension=("sku", "day"),
                                                 metrics=METRICS_REPORT)
        allowed = self.in_stock_offers() if only_in_stock else None
        result = P.rows_to_daily(rows, sku_map, order, allowed, self.exclude_marker)
        # подмешиваем расход рекламы
        ads = self.ad_spend(df, dt)
        if ads:
            P.merge_ad_spend(result, ads, sku_map)
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
        """{ключ_товара: {день: расход}} — с кэшем по периоду."""
        if not self.perf:
            return {}
        key = (date_from, date_to)
        if key in self._ad_cache:
            return self._ad_cache[key]
        try:
            data = self.perf.spend_by_product_day(date_from, date_to)
        except Exception as e:
            log.warning("[%s] реклама недоступна: %s", self.name, e)
            data = {}
        self._ad_cache[key] = data
        return data

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
