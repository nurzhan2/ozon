# -*- coding: utf-8 -*-
"""
Обработка данных: маппинг SKU->артикул, фильтр OUT, только товары на остатках,
расчёт качественных показателей (KPI), сравнение периодов (день-ко-дню).
"""


def is_excluded(offer_id, marker="OUT"):
    """True, если артикул содержит метку (напр. 'OUT') — такой товар не включаем."""
    if not offer_id:
        return False
    return marker.upper() in str(offer_id).upper()


def safe_div(a, b):
    a = a or 0
    b = b or 0
    return (a / b) if b else 0.0


def rows_to_products(analytics_rows, sku_map, metric_order):
    """
    Преобразует строки аналитики (dimension = ['sku'] или ['sku','day'])
    в агрегат по товарам: offer_id -> {name, product_id, <метрики (сумма)>}.
    Если день присутствует в измерениях — суммирует по дням.
    Товары, для которых нет маппинга sku->offer_id, помечаются как unknown.
    """
    products = {}
    for row in analytics_rows:
        dims = row.get("dimensions", [])
        if not dims:
            continue
        sku_raw = dims[0].get("id")
        try:
            sku = int(sku_raw)
        except (TypeError, ValueError):
            sku = sku_raw
        info = sku_map.get(sku)
        if info:
            offer_id = info["offer_id"]
            name = info["name"]
            product_id = info["product_id"]
        else:
            # маппинг не найден — используем название из аналитики, offer_id пустой
            offer_id = ""
            name = dims[0].get("name", "") or str(sku_raw)
            product_id = None

        key = offer_id or f"sku:{sku_raw}"
        rec = products.setdefault(key, {
            "offer_id": offer_id,
            "name": name,
            "product_id": product_id,
            "sku": sku_raw,
        })
        for m in metric_order:
            rec[m] = rec.get(m, 0) + (row.get(m, 0) or 0)
    return products


def rows_to_daily(analytics_rows, sku_map, metric_order, allowed_offers=None, marker="OUT"):
    """
    Строки аналитики с измерениями (sku, day) -> подневная структура:
       {offer_id: {"name":..., "sku":..., "days": {"YYYY-MM-DD": {метрика: значение}}}}
    allowed_offers — множество артикулов на остатках (None = не фильтровать).
    Артикулы с меткой marker отбрасываются всегда.
    """
    result = {}
    for row in analytics_rows:
        dims = row.get("dimensions", [])
        if len(dims) < 2:
            continue
        sku_raw = dims[0].get("id")
        day = dims[1].get("id")
        try:
            sku = int(sku_raw)
        except (TypeError, ValueError):
            sku = sku_raw
        info = sku_map.get(sku)
        offer_id = info["offer_id"] if info else ""
        name = info["name"] if info else (dims[0].get("name", "") or str(sku_raw))

        if is_excluded(offer_id, marker):
            continue
        if allowed_offers is not None and offer_id not in allowed_offers:
            continue

        key = offer_id or f"sku:{sku_raw}"
        rec = result.setdefault(key, {"offer_id": offer_id, "name": name,
                                      "sku": sku_raw, "days": {}})
        d = rec["days"].setdefault(day, {})
        for m in metric_order:
            d[m] = d.get(m, 0) + (row.get(m, 0) or 0)
    return result


def merge_ad_spend(daily, ad_spend, sku_map):
    """
    Добавляет метрику 'ad_spend' в подневную структуру.
    ad_spend: {ключ: {день: расход}}, ключом может быть sku или артикул.
    """
    # индекс: и по sku, и по артикулу
    by_offer = {}
    for key, days in ad_spend.items():
        offer = key
        try:
            info = sku_map.get(int(key))
            if info:
                offer = info["offer_id"]
        except (TypeError, ValueError):
            pass
        tgt = by_offer.setdefault(offer, {})
        for day, val in days.items():
            tgt[day] = tgt.get(day, 0.0) + val

    for key, rec in daily.items():
        spend_days = by_offer.get(rec.get("offer_id")) or by_offer.get(str(rec.get("sku"))) or {}
        for day, d in rec["days"].items():
            d["ad_spend"] = spend_days.get(day, 0.0)


def _index_by_offer(data, sku_map):
    """
    Приводит {ключ: {...}} к артикулам: ключом может быть sku или артикул.
    Та же логика, что в merge_ad_spend, вынесена отдельно.
    """
    out = {}
    for key, val in (data or {}).items():
        offer = key
        try:
            info = sku_map.get(int(key))
            if info:
                offer = info["offer_id"]
        except (TypeError, ValueError):
            pass
        out.setdefault(offer, {}).update(val if isinstance(val, dict) else {})
        if not isinstance(val, dict):
            out[offer] = val
    return out


def _rec_keys(rec):
    """Под какими ключами товар может быть в чужих данных."""
    return [k for k in (rec.get("offer_id"), str(rec.get("sku") or "")) if k]


def merge_queries(daily, queries_by_day, sku_map):
    """
    Подмешивает данные /v1/analytics/product-queries:
      views    -> hits_view          («показы»: уникальные посетители)
      position -> position_category  («место в поиске»)

    Метрики того же смысла из /v1/analytics/data доступны только с Premium
    Plus, а этот метод работает с обычным Premium.
    """
    by_offer = {}
    for day, items in (queries_by_day or {}).items():
        for sku, rec in (items or {}).items():
            offer = rec.get("offer_id") or ""
            if not offer:
                info = None
                try:
                    info = sku_map.get(int(sku))
                except (TypeError, ValueError):
                    pass
                offer = info["offer_id"] if info else sku
            for key in {offer, str(sku)}:
                by_offer.setdefault(key, {})[day] = rec

    for rec in daily.values():
        src = {}
        for k in _rec_keys(rec):
            if k in by_offer:
                src = by_offer[k]
                break
        for day, d in rec["days"].items():
            q = src.get(day)
            if not q:
                continue
            if q.get("views"):
                d["hits_view"] = q["views"]
            if q.get("position"):
                d["position_category"] = q["position"]


def merge_ad_traffic(daily, ad_stats, sku_map):
    """
    Показы и клики из рекламного отчёта: ad_views / ad_clicks, а также
    session_view («клики») — другого источника кликов без Premium Plus нет.
    ВАЖНО: это трафик только по рекламируемым товарам, не весь.
    """
    by_offer = {}
    for key, days in (ad_stats or {}).items():
        offer = key
        try:
            info = sku_map.get(int(key))
            if info:
                offer = info["offer_id"]
        except (TypeError, ValueError):
            pass
        tgt = by_offer.setdefault(offer, {})
        for day, v in days.items():
            acc = tgt.setdefault(day, {"views": 0.0, "clicks": 0.0})
            acc["views"] += v.get("views", 0.0)
            acc["clicks"] += v.get("clicks", 0.0)

    for rec in daily.values():
        src = {}
        for k in _rec_keys(rec):
            if k in by_offer:
                src = by_offer[k]
                break
        for day, d in rec["days"].items():
            v = src.get(day) or {}
            d["ad_views"] = v.get("views", 0.0)
            d["ad_clicks"] = v.get("clicks", 0.0)
            # «клики» в отчёте — рекламные: store-wide кликов без Premium Plus нет
            d["session_view"] = v.get("clicks", 0.0)


def merge_cancels(daily, cancels, sku_map):
    """Отменённые штуки из отправлений -> метрика cancellations."""
    by_offer = {}
    for key, days in (cancels or {}).items():
        offer = key
        try:
            info = sku_map.get(int(key))
            if info:
                offer = info["offer_id"]
        except (TypeError, ValueError):
            pass
        tgt = by_offer.setdefault(offer, {})
        for day, qty in days.items():
            tgt[day] = tgt.get(day, 0) + qty

    for rec in daily.values():
        src = {}
        for k in _rec_keys(rec):
            if k in by_offer:
                src = by_offer[k]
                break
        for day, d in rec["days"].items():
            if src.get(day):
                d["cancellations"] = src[day]


def sum_days(rec, days, metric):
    """Сумма метрики по списку дней в подневной записи."""
    total = 0
    for day in days:
        total += (rec["days"].get(day, {}).get(metric, 0) or 0)
    return total


def add_kpis(rec):
    """Добавляет производные качественные показатели в запись товара."""
    views = rec.get("hits_view", 0)
    sessions = rec.get("session_view", 0)
    tocart = rec.get("hits_tocart", 0)
    ordered = rec.get("ordered_units", 0)
    revenue = rec.get("revenue", 0)

    # Конверсия из просмотра карточки в заказ (шт / сессии), в %
    rec["conv_view_to_order_%"] = round(safe_div(ordered, sessions) * 100, 2)
    # Конверсия в корзину (если не пришла из API) — добавления/сессии, %
    if not rec.get("conv_tocart"):
        rec["conv_tocart"] = round(safe_div(tocart, sessions) * 100, 2)
    # Средний чек, руб
    rec["avg_check"] = round(safe_div(revenue, ordered), 2)
    return rec


def filter_products(products, stocks, marker="OUT", only_in_stock=True):
    """
    Фильтрует товары:
      - убирает артикулы с меткой marker (OUT);
      - если only_in_stock — оставляет только те, у кого present>0 в остатках.
    Добавляет в запись остатки (stock_present / stock_reserved).
    """
    out = {}
    for key, rec in products.items():
        offer_id = rec.get("offer_id", "")
        if is_excluded(offer_id, marker):
            continue
        st = stocks.get(offer_id, {})
        present = st.get("present", 0)
        reserved = st.get("reserved", 0)
        rec["stock_present"] = present
        rec["stock_reserved"] = reserved
        # stocks пустой — значит остатки не пришли вовсе; фильтровать по ним
        # нельзя, иначе отчёт молча обнулится (см. in_stock_offers).
        if only_in_stock and stocks and present <= 0:
            continue
        out[key] = rec
    return out


def stock_rows(stocks, offer_names, marker="OUT"):
    """
    Готовит строки отчёта по остаткам (только товары с present>0, без OUT).
    offer_names: offer_id -> name.
    """
    rows = []
    for offer_id, st in stocks.items():
        if is_excluded(offer_id, marker):
            continue
        if st.get("present", 0) <= 0:
            continue
        rows.append({
            "offer_id": offer_id,
            "name": offer_names.get(offer_id, ""),
            "present": st.get("present", 0),
            "reserved": st.get("reserved", 0),
        })
    rows.sort(key=lambda r: r["present"], reverse=True)
    return rows


def compare_day_over_day(today_products, prev_products, metrics):
    """
    Сравнивает два набора товаров по метрикам (сегодня vs вчера / вчера vs позавчера).
    Возвращает список строк с парами значений и дельтами.
    Ключ сопоставления — offer_id (или sku, если offer_id пуст).
    """
    keys = set(today_products) | set(prev_products)
    rows = []
    for key in keys:
        t = today_products.get(key, {})
        p = prev_products.get(key, {})
        base = t or p
        row = {
            "offer_id": base.get("offer_id", ""),
            "name": base.get("name", ""),
            "stock_present": base.get("stock_present", 0),
        }
        for m in metrics:
            tv = t.get(m, 0) or 0
            pv = p.get(m, 0) or 0
            row[f"{m}_today"] = tv
            row[f"{m}_prev"] = pv
            row[f"{m}_delta"] = round(tv - pv, 2)
            row[f"{m}_delta_%"] = round(safe_div(tv - pv, pv) * 100, 2) if pv else None
        rows.append(row)
    # сортировка по заказам сегодня (если метрика есть)
    sort_key = "ordered_units_today" if "ordered_units" in metrics else None
    if sort_key:
        rows.sort(key=lambda r: r.get(sort_key, 0) or 0, reverse=True)
    return rows


def merge_cabinet(daily, cab, sku_map):
    """
    Выгрузка из личного кабинета -> недостающие метрики.

      views    -> hits_view          «показы»
      sessions -> session_view       «клики» (настоящие, не только рекламные)
      tocart   -> hits_tocart        «корзина»
      position -> position_category  «место в поиске»

    Кабинет — единственный источник корзины: в API её нет ни в одном методе
    без подписки Premium Plus. Значения кабинета ПЕРЕКРЫВАЮТ то, что уже
    подмешано из рекламы и из product-queries: там суррогаты, здесь то же,
    что видит клиент у себя в интерфейсе.

    Нули из файла не перекрывают ничего: пустая клетка в выгрузке и честный
    ноль в кабинете выглядят одинаково, а занулять уже собранное нельзя.

    Возвращает множество ключей, которые реально заполнились, — по нему
    отчёт решает, называть строку «клики» или «клики (реклама)».
    """
    filled = set()
    by_offer = {}
    for key, days in (cab or {}).items():
        offer = key
        try:
            info = sku_map.get(int(key))
            if info:
                offer = info["offer_id"]
        except (TypeError, ValueError):
            pass
        by_offer.setdefault(offer, {}).update(days)
        by_offer.setdefault(str(key), {}).update(days)

    pairs = (("views", "hits_view"), ("sessions", "session_view"),
             ("tocart", "hits_tocart"), ("position", "position_category"))

    for rec in daily.values():
        src = {}
        for k in _rec_keys(rec):
            if k in by_offer:
                src = by_offer[k]
                break
        for day, d in rec["days"].items():
            row = src.get(day)
            if not row:
                continue
            for role, metric in pairs:
                if row.get(role):
                    d[metric] = row[role]
                    filled.add(metric)
    return filled
