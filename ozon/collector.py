# -*- coding: utf-8 -*-
"""
Сбор данных по одному магазину: справочник товаров, остатки (в т.ч. по кластерам),
подневная аналитика, расход рекламы по товарам.
Справочники кэшируются в рамках одного запуска.
"""

import logging
import os

from .seller_api import SellerAPI, SellerAPIError, METRICS_REPORT
from .performance_api import (PerformanceAPI, CACHE_DIR, _safe_name,
                              _read_json, _write_json)
from . import processing as P
from . import cabinet as CAB
from . import dates as D

log = logging.getLogger("ozon.collector")


def _cluster_map_path(store_name):
    """Где лежит карта «склад -> кластер». DATA_DIR переживает деплой."""
    return os.path.join(CACHE_DIR, f"clusters_{_safe_name(store_name)}.json")


def _days_between(date_from, date_to):
    """Список дней 'YYYY-MM-DD' включительно."""
    from datetime import date as _date, timedelta as _td
    try:
        a = _date.fromisoformat(str(date_from)[:10])
        b = _date.fromisoformat(str(date_to)[:10])
    except ValueError:
        return []
    out, cur = [], a
    while cur <= b:
        out.append(cur.isoformat())
        cur += _td(days=1)
    return out


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
        # Запросы товаров (показы и позиция) — по дням, потому что отчёты
        # показывают их подневно. Кэш нужен: отчёты 1 и 3 берут один период.
        self._queries_cache = {}
        self._cabinet = None          # выгрузка кабинета, читается лениво
        self.cabinet_filled = set()   # какие метрики она реально закрыла
        # Дни, за которые источник вообще что-то отдал. Нужны отчёту, чтобы
        # отличить «ноль показов» от «данных ещё нет»: OZON считает показы
        # с задержкой в день-два, и на свежей колонке ноль был бы враньём.
        self.days_with_views = set()
        self.days_with_cart = set()
        # взводится после двух отказов product-queries подряд, см. queries_by_day
        self._queries_off = False
        # Отмены из отправлений — по периодам.
        self._cancel_cache = {}

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
                self._save_cluster_map(rows)
            except SellerAPIError as e:
                log.warning("[%s] кластерный метод недоступен (%s), беру склады",
                            self.name, e)
                rows = self._warehouses_as_clusters()
            names = self.offer_names()
            for r in rows:
                if not r.get("name"):
                    r["name"] = names.get(r.get("offer_id", ""), "")
            self._cluster_rows = [
                r for r in rows
                if not P.is_excluded(r.get("offer_id", ""), self.exclude_marker)
            ]
        return self._cluster_rows

    def _save_cluster_map(self, rows):
        """
        Запоминает, какой склад к какому кластеру относится, и среднесуточные
        продажи кластера. Карта меняется редко — OZON не переносит склады
        между кластерами каждый день, — поэтому вчерашняя годится сегодня.
        """
        wh, ads = {}, {}
        for r in rows:
            cluster = (r.get("cluster") or "").strip()
            if not cluster:
                continue
            w = (r.get("warehouse") or "").strip()
            if w:
                wh[w] = cluster
            key = f"{r.get('offer_id', '')}\t{cluster}"
            if key not in ads:
                ads[key] = [r.get("ads") or 0.0, r.get("idc") or 0.0,
                            r.get("ads_all") or 0.0]
        if wh:
            _write_json(_cluster_map_path(self.name), {"warehouses": wh, "ads": ads})

    def _warehouses_as_clusters(self):
        """
        Запасной путь, когда /v1/analytics/stocks отбил 429 все семь попыток.

        Раньше сюда просто подставлялось имя склада вместо кластера, и в
        отчёте вместо «Москва, МО и Дальние регионы» появлялись УФА_РФЦ,
        ПУШКИНО_2_РФЦ и ещё три десятка строк на товар — заказчик это увидел
        первым же утром. Остатки при этом были правильные: сломалась только
        группировка.

        Поэтому берём свежие остатки по складам, а группировку — из карты,
        сохранённой в прошлый удачный прогон. Оттуда же ads_cluster, иначе
        «прод 7д» и потребность считать не от чего.
        """
        rows = self.seller.stocks_on_warehouses()
        cache = _read_json(_cluster_map_path(self.name), {}) or {}
        wh = cache.get("warehouses") or {}
        ads = cache.get("ads") or {}
        if not wh:
            log.warning("[%s] карты «склад -> кластер» ещё нет — в отчёте "
                        "будут склады. Появится после первого удачного "
                        "кластерного прогона", self.name)
            return rows

        unknown, merged = set(), {}
        for r in rows:
            w = (r.get("warehouse") or "").strip()
            cluster = wh.get(w)
            if not cluster:
                unknown.add(w)
                cluster = w
            key = (r.get("offer_id", ""), cluster)
            cur = merged.get(key)
            if cur is None:
                a = ads.get(f"{r.get('offer_id', '')}\t{cluster}") or [0.0, 0.0, 0.0]
                r = dict(r, cluster=cluster, ads=a[0], idc=a[1], ads_all=a[2])
                merged[key] = r
                continue
            for f in ("available", "requested", "transit"):
                cur[f] += r.get(f, 0) or 0

        log.info("[%s] кластеры восстановлены из сохранённой карты: складов "
                 "%d -> строк %d%s", self.name, len(rows), len(merged),
                 f", без кластера осталось складов: {len(unknown)}" if unknown else "")
        return list(merged.values())

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
        ads = self.ad_stats(df, dt)
        if ads:
            P.merge_ad_spend(result, {k: {d: v.get("spend", 0.0)
                                          for d, v in days.items()}
                                      for k, days in ads.items()}, sku_map)
            # показы и клики по рекламе — единственный доступный источник
            # кликов без подписки Premium Plus
            P.merge_ad_traffic(result, ads, sku_map)
        # показы и место в поиске из «запросов моих товаров» (обычный Premium)
        q = self.queries_by_day(df, dt)
        if q:
            P.merge_queries(result, q, sku_map)
            self.days_with_views |= {d for d, items in q.items() if items}
        # отмены из отправлений — точные, без подписки
        c = self.cancels(df, dt)
        if c:
            P.merge_cancels(result, c, sku_map)
        # выгрузка из кабинета — идёт ПОСЛЕДНЕЙ и перекрывает суррогаты:
        # это единственный источник корзины и самые верные показы с кликами
        cab = self.cabinet_data()
        if cab:
            self.cabinet_filled = P.merge_cabinet(result, cab, sku_map)
            cab_days = {day for days in cab.values() for day in days}
            self.days_with_views |= cab_days
            if "hits_tocart" in self.cabinet_filled:
                self.days_with_cart |= cab_days
        self._daily_cache[key] = (result, order)
        return result, order

    def cabinet_data(self):
        """Метрики из выгрузки кабинета. Папка читается один раз за прогон."""
        return self._cabinet_all().get("metrics") or {}

    def cabinet_orders(self):
        """
        {артикул: {кластер доставки: {день: штук}}} из выгрузки заказов.

        Нужны отчёту по остаткам: потребность считается по кластеру ДОСТАВКИ,
        а ads_cluster из API привязан к кластеру отгрузки — это разные вещи.
        """
        return self._cabinet_all().get("orders") or {}

    def _cabinet_all(self):
        if self._cabinet is None:
            try:
                self._cabinet = CAB.load(self.name, self.cfg) or CAB._empty()
            except Exception as e:      # источник необязательный, сбор важнее
                log.warning("[%s] выгрузку кабинета прочитать не удалось: %s",
                            self.name, str(e)[:200])
                self._cabinet = CAB._empty()
        return self._cabinet

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

    # ---------------- замена метрик Premium Plus ----------------
    def queries_by_day(self, date_from, date_to):
        """
        {день: {sku: {views, position, ...}}} из /v1/analytics/product-queries.

        Метод доступен с обычным Premium, поэтому им закрываются «показы»
        (уникальные посетители, увидевшие товар) и «место в поиске», которых
        в /v1/analytics/data нет без Premium Plus. Запрашиваем по дню, потому
        что отчёты показывают их подневно; каждый день кэшируется.

        Сегодняшний день OZON не считает («расчёт идёт 1-2 дня») — пропускаем.

        Метод капризен: на одно и то же окно он отвечает то данными, то
        «There is no data for the specified period», причём отказ приходит
        и на те дни, за которые данные заведомо есть. Разобраться, от чего
        это зависит, пока не удалось (проверены формат дат, длина окна,
        выравнивание по неделям, частота запросов — ни одна версия не
        подтвердилась). Поэтому здесь стоит предохранитель: после двух
        отказов подряд магазин перестаёт опрашиваться до конца прогона.
        Иначе на пустом месте уходит по запросу на каждый день периода,
        а лог заполняется одинаковыми предупреждениями.
        """
        sku_map, _ = self.maps()
        skus = list(sku_map)
        if not skus:
            return {}
        today = D.d(D.today(getattr(self.cfg, "tz", "Europe/Moscow"))
                    if hasattr(self.cfg, "tz") else D.today())
        out = {}
        misses = 0
        for day in _days_between(date_from, date_to):
            if day >= today:
                continue
            if day not in self._queries_cache:
                if self._queries_off:
                    continue
                try:
                    self._queries_cache[day] = self.seller.product_queries(
                        day, day, skus)
                    misses = 0
                except SellerAPIError as e:
                    misses += 1
                    # 200 символов обрезали ответ OZON ровно на том месте, где
                    # начиналась причина отказа: «desc = There is n...».
                    log.warning("[%s] запросы товаров за %s недоступны: %s",
                                self.name, day, str(e)[:600])
                    self._queries_cache[day] = {}
                    if misses >= 2:
                        self._queries_off = True
                        log.warning(
                            "[%s] product-queries отказывает подряд — "
                            "больше не спрашиваю в этом прогоне. Строки "
                            "«показы» и «место в поиске» останутся пустыми.",
                            self.name)
            out[day] = self._queries_cache.get(day, {})
        return out

    def cancels(self, date_from, date_to):
        """{ключ_товара: {день: отменено_шт}} из отправлений FBO и FBS."""
        key = (date_from, date_to)
        if key not in self._cancel_cache:
            try:
                self._cancel_cache[key] = self.seller.cancelled_units(
                    date_from, date_to)
            except SellerAPIError as e:
                log.warning("[%s] отмены недоступны: %s", self.name, str(e)[:200])
                self._cancel_cache[key] = {}
        return self._cancel_cache[key]

    # ---------------- реклама ----------------
    def ad_spend(self, date_from, date_to):
        """{ключ_товара: {день: расход}} — только деньги, для «рекламы» и ДРР."""
        return {sku: {day: v.get("spend", 0.0) for day, v in days.items()}
                for sku, days in self.ad_stats(date_from, date_to).items()}

    def ad_stats(self, date_from, date_to):
        """
        {ключ_товара: {день: {spend, views, clicks}}} за запрошенный период.

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
            self._ad_data = self.perf.stats_by_product_day(need_from, need_to)
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
