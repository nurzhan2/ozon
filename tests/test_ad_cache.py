# -*- coding: utf-8 -*-
"""Проверка: сколько раз за утренний пакет collector идёт в Performance API."""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

from ozon.collector import StoreCollector, _slice_days

ok = True
def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


class FakePerf:
    """Отдаёт по 10 руб. в день на два товара, считает походы."""
    def __init__(self):
        self.sweeps = []
        self.last_spend_dated = True

    def spend_by_product_day(self, df, dt):
        from datetime import date, timedelta
        self.sweeps.append((df, dt))
        out, cur = {}, date.fromisoformat(df)
        end = date.fromisoformat(dt)
        while cur <= end:
            for sku in ("SKU1", "SKU2"):
                out.setdefault(sku, {})[cur.isoformat()] = 10.0
            cur += timedelta(days=1)
        return out


def make():
    c = StoreCollector.__new__(StoreCollector)
    c.name = "ТЕСТ"
    c.perf = FakePerf()
    c._ad_data, c._ad_range, c._ad_dated = {}, None, True
    return c


print("\n1. Утренний пакет: месяц, потом вчера, потом позавчера")
c = make()
whole = c.ad_spend("2026-08-01", "2026-08-04")     # отчёт 1 и 3
y = c.ad_spend("2026-08-04", "2026-08-04")         # отчёт 2, текущий день
dby = c.ad_spend("2026-08-03", "2026-08-03")       # отчёт 2, предыдущий день
check("поход в API ровно один", len(c.perf.sweeps) == 1, c.perf.sweeps)
check("месяц: 4 дня на товар", len(whole["SKU1"]) == 4, whole["SKU1"])
check("вчера: только 04.08", list(y["SKU1"]) == ["2026-08-04"], y["SKU1"])
check("позавчера: только 03.08", list(dby["SKU1"]) == ["2026-08-03"], dby["SKU1"])
check("суммы не поехали", y["SKU1"]["2026-08-04"] == 10.0, y)

print("\n2. Запрос шире собранного — объединение, а не два куска")
c = make()
c.ad_spend("2026-08-03", "2026-08-04")
c.ad_spend("2026-08-01", "2026-08-02")
check("второй поход покрыл весь диапазон",
      c.perf.sweeps[-1] == ("2026-08-01", "2026-08-04"), c.perf.sweeps)
check("походов всего два", len(c.perf.sweeps) == 2, c.perf.sweeps)
later = c.ad_spend("2026-08-04", "2026-08-04")
check("третий отрезок берётся из кэша", len(c.perf.sweeps) == 2, c.perf.sweeps)
check("и он не пустой", later["SKU1"] == {"2026-08-04": 10.0}, later)

print("\n3. Реклама отвалилась — не долбим API повторно")
class Broken(FakePerf):
    def spend_by_product_day(self, df, dt):
        self.sweeps.append((df, dt))
        raise RuntimeError("429 дневной лимит")

c = make(); c.perf = Broken()
a = c.ad_spend("2026-08-01", "2026-08-04")
b = c.ad_spend("2026-08-04", "2026-08-04")
check("пустой результат вместо падения", a == {} and b == {}, (a, b))
check("повторного похода нет", len(c.perf.sweeps) == 1, c.perf.sweeps)

print("\n4. Отчёт без колонки дат не режется по дням")
class NoDates(FakePerf):
    def spend_by_product_day(self, df, dt):
        self.sweeps.append((df, dt))
        self.last_spend_dated = False
        return {"SKU1": {df: 120.0}}

c = make(); c.perf = NoDates()
full = c.ad_spend("2026-08-01", "2026-08-04")
one = c.ad_spend("2026-08-04", "2026-08-04")
check("расход не обнулился при нарезке", one == full == {"SKU1": {"2026-08-01": 120.0}}, one)
check("второго похода нет", len(c.perf.sweeps) == 1, c.perf.sweeps)

print("\n5. _slice_days не тащит пустые товары")
src = {"A": {"2026-08-01": 5.0}, "B": {"2026-08-09": 7.0}}
check("товар без дней в периоде выпадает",
      _slice_days(src, "2026-08-01", "2026-08-05") == {"A": {"2026-08-01": 5.0}})

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
