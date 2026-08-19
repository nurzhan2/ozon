# -*- coding: utf-8 -*-
"""
Выгрузка кабинета закрывает НЕ ВСЕ строки — незакрытые остаются пустыми.

Набор, который реально выгружает заказчик: «Уникальные посетители, всего»,
«Уникальные посетители с просмотром карточки товара», «Конверсия в корзину
из карточки товара», «Позиция в поиске и каталоге». Колонки ПОКАЗОВ там нет
вовсе — в конструкторе кабинета такой метрики не предлагают.

Раньше признак «за этот день выгрузка что-то дала» ставился на все дни файла
целиком. С этим набором строка «показы» напечаталась бы нулём рядом с пятью
тысячами кликов, а CTR — 0,0%. Ноль показов означает «товар никто не видел»,
и это враньё. Заказчик такое уже ловил один раз.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OZON_STORES",
                      '[{"name":"ТЕСТ","client_id":"1","api_key":"k"}]')

from ozon import processing as P
from ozon import reports as R

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


DAY = "2026-08-18"


def daily():
    return {"ART-1": {"offer_id": "ART-1", "name": "Товар", "sku": "111",
                      "days": {DAY: {"revenue": 90000, "ordered_units": 300,
                                     "ad_spend": 13000, "ad_views": 9000,
                                     "ad_clicks": 180}}}}


def values(cab):
    d = daily()
    filled = P.merge_cabinet(d, cab, {})
    v = R._quality_day_values(
        d["ART-1"]["days"][DAY],
        unified=("session_view" in filled),
        has_views=("hits_view" in filled),
        has_cart=("hits_tocart" in filled),
        has_position=("position_category" in filled))
    return filled, v


print("\n1. Набор заказчика: показов нет, остальное есть")
REAL = {"ART-1": {DAY: {"sessions": 5000.0, "sessions_pdp": 2000.0,
                        "tocart": 740.0, "position": 12.0}}}
filled, v = values(REAL)
check("выгрузка закрыла клики, корзину и позицию",
      sorted(filled) == ["hits_tocart", "position_category", "session_view"],
      sorted(filled))
check("показы ПУСТЫЕ, а не ноль", v["hits_view"] is None, v["hits_view"])
check("CTR пустой, а не 0,0%", v["ctr"] is None, v["ctr"])
check("клики на месте", v["session_view"] == 5000, v["session_view"])
check("корзина на месте", v["hits_tocart"] == 740, v["hits_tocart"])
check("% корзины считается от кликов",
      abs(v["cart_rate"] - 0.148) < 0.001, v["cart_rate"])
check("МЕСТО В ПОИСКЕ не обнулилось вместе с показами",
      v["position_category"] == 12, v["position_category"])

print("\n2. Если показы в файле есть — считается всё")
WITH_VIEWS = {"ART-1": {DAY: dict(REAL["ART-1"][DAY], views=40000.0)}}
filled, v = values(WITH_VIEWS)
check("показы заполнены", v["hits_view"] == 40000, v["hits_view"])
check("CTR считается по своей паре",
      abs(v["ctr"] - 0.125) < 0.001, v["ctr"])

print("\n3. Признаки дней разделены: показы, корзина, позиция — по отдельности")


class Col:
    """Сборщик с настоящей логикой полей, но без сети."""
    name = "ТЕСТ"

    def __init__(self):
        self.days_with_views = set()
        self.days_with_cart = set()
        self.days_with_position = set()
        self.cabinet_filled = set()

    def absorb(self, cab):
        d = daily()
        self.cabinet_filled = P.merge_cabinet(d, cab, {})
        cab_days = {day for days in cab.values() for day in days}
        if "hits_view" in self.cabinet_filled:
            self.days_with_views |= cab_days
        if "hits_tocart" in self.cabinet_filled:
            self.days_with_cart |= cab_days
        if "position_category" in self.cabinet_filled:
            self.days_with_position |= cab_days


c = Col()
c.absorb(REAL)
check("день НЕ попал в дни с показами", c.days_with_views == set(), c.days_with_views)
check("день попал в дни с корзиной", c.days_with_cart == {DAY}, c.days_with_cart)
check("день попал в дни с позицией", c.days_with_position == {DAY},
      c.days_with_position)

print("\n4. Так же устроено и в живом сборщике")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ozon", "collector.py"), encoding="utf-8").read()
code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
i = next(n for n, l in enumerate(code)
         if l.strip() == "self.days_with_views |= cab_days")
guard = next(l for l in reversed(code[:i]) if l.strip())
check("присвоение days_with_views стоит под проверкой hits_view",
      guard.strip().startswith('if "hits_view" in self.cabinet_filled')
      and (len(l) - len(l.lstrip()) for l in [code[i]]),
      guard.strip())
check("признак показов проверяется по hits_view",
      any('"hits_view" in self.cabinet_filled' in l for l in code),
      [l for l in code if "hits_view" in l])
check("признак позиции существует",
      any("days_with_position" in l for l in code))

print("\n5. По умолчанию позиция ведёт себя как раньше")
v = R._quality_day_values({"position_category": 7}, has_views=True)
check("has_position не задан -> берётся has_views", v["position_category"] == 7,
      v["position_category"])
v = R._quality_day_values({"position_category": 7}, has_views=False)
check("показов нет и признака позиции нет -> пусто",
      v["position_category"] is None, v["position_category"])

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
