# -*- coding: utf-8 -*-
"""
Снимки (snapshots) внутридневных данных.

OZON отдаёт аналитику ПОДНЕВНО (без разбивки по часам). Поэтому, чтобы
сравнивать "сегодня на 12:00 к вчера на 12:00", инструмент сохраняет снимок
накопленных за день значений в каждый запуск (по слоту часа) и на следующий
день сравнивает текущие цифры со вчерашним снимком того же слота.
"""

import os
import json


def _path(base_dir, store_name, day, hour):
    safe = "".join(ch if ch.isalnum() else "_" for ch in store_name)
    return os.path.join(base_dir, f"{safe}__{day}__{hour:02d}.json")


SNAP_KEYS = ("revenue", "ordered_units", "ad_spend", "drr", "hits_view")


def save(base_dir, store_name, day, hour, rows_by_name):
    """
    rows_by_name: {название товара: {revenue, ordered_units, ad_spend, drr, hits_view}}
    Сохраняется срез накопленных за день значений на текущий час.
    """
    os.makedirs(base_dir, exist_ok=True)
    slim = {name: {k: vals.get(k, 0) for k in SNAP_KEYS}
            for name, vals in rows_by_name.items()}
    with open(_path(base_dir, store_name, day, hour), "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)


def load(base_dir, store_name, day, hour):
    p = _path(base_dir, store_name, day, hour)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
