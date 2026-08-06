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
