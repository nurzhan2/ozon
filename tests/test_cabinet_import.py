# -*- coding: utf-8 -*-
"""Импорт выгрузок из личного кабинета: разбор файла и заполнение отчёта."""
import io
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OZON_STORES",
                      '[{"name":"ТЕСТ","client_id":"1","api_key":"k"}]')

from openpyxl import Workbook, load_workbook

from ozon import cabinet as CAB
from ozon import processing as P
from ozon import reports as R

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


print("\n1. Шапка ищется по названиям, а не по номеру строки")
rows = [
    ["Отчёт по товарам"],
    ["Период: 05.08.2026 — 06.08.2026"],
    [],
    ["Дата", "Артикул", "Ozon ID", "Показы", "Сессии", "В корзину", "Позиция"],
    ["05.08.2026", "ART-1", "111", "12 500", "3 040", "621", "7"],
    ["06.08.2026", "ART-1", "111", "11 200", "2 800", "590", "9"],
]
out = CAB.parse_rows(rows, "тест")
check("две служебные строки сверху не помешали", "ART-1" in out, list(out))
check("пробелы в числах разобраны",
      out["ART-1"]["2026-08-05"]["views"] == 12500.0,
      out["ART-1"]["2026-08-05"])
check("дата ДД.ММ.ГГГГ приведена к ISO",
      set(out["ART-1"]) == {"2026-08-05", "2026-08-06"}, list(out["ART-1"]))
check("корзина на месте", out["ART-1"]["2026-08-06"]["tocart"] == 590.0)
check("позиция берётся как есть, не суммируется",
      out["ART-1"]["2026-08-06"]["position"] == 9.0)

print("\n2. Другие формулировки колонок — тот же результат")
rows2 = [
    ["День", "Ваш SKU", "Показы всего", "Добавления в корзину",
     "Средняя позиция"],
    ["2026-08-05", "ART-9", "1000", "50", "3,5"],
]
out2 = CAB.parse_rows(rows2, "тест2")
check("«Показы всего» распознано", out2["ART-9"]["2026-08-05"]["views"] == 1000.0)
check("«Добавления в корзину» распознано",
      out2["ART-9"]["2026-08-05"]["tocart"] == 50.0)
check("запятая как десятичный разделитель",
      out2["ART-9"]["2026-08-05"]["position"] == 3.5)

print("\n3. Строки одного дня складываются, «Итого» отбрасывается")
rows3 = [
    ["Дата", "Артикул", "Показы", "В корзину"],
    ["2026-08-05", "ART-1", "100", "5"],
    ["2026-08-05", "ART-1", "50", "3"],
    ["2026-08-05", "Итого", "150", "8"],
]
out3 = CAB.parse_rows(rows3, "тест3")
check("две строки за день сложились",
      out3["ART-1"]["2026-08-05"]["views"] == 150.0, out3["ART-1"])
check("строка «Итого» не стала товаром", "Итого" not in out3, list(out3))

print("\n4. Выгрузка без разбивки по дням отвергается внятно")
try:
    CAB.parse_rows([["Артикул", "Показы", "В корзину"],
                    ["ART-1", "100", "5"]], "без_дат.xlsx")
    check("должно было подняться исключение", False)
except CAB.CabinetImportError as e:
    check("сказано, что нужна разбивка по дням",
          "ПО ДНЯМ" in str(e), str(e)[:120])

print("\n5. Файл без артикула и без sku тоже отвергается")
try:
    CAB.parse_rows([["Дата", "Показы"], ["2026-08-05", "100"]], "без_ключа")
    check("должно было подняться исключение", False)
except CAB.CabinetImportError as e:
    check("сказано, что не с чем сопоставить", "сопоставить" in str(e), str(e))

print("\n6. csv с точкой с запятой")
data = "Дата;Артикул;Показы;В корзину\n2026-08-05;ART-2;300;12\n".encode("utf-8")
out6 = CAB.parse_file(data, "v.csv")
check("csv разобран", out6["ART-2"]["2026-08-05"]["tocart"] == 12.0, out6)

print("\n7. xlsx читается по байтам")
wb = Workbook()
ws = wb.active
ws.append(["Дата", "Артикул", "Показы", "В корзину"])
ws.append([_dt.date(2026, 8, 5), "ART-3", 700, 33])
buf = io.BytesIO()
wb.save(buf)
out7 = CAB.parse_file(buf.getvalue(), "v.xlsx")
check("дата-объект из ячейки понята",
      out7["ART-3"]["2026-08-05"]["views"] == 700.0, out7)

print("\n8. merge_cabinet перекрывает суррогаты, но не зануляет")
daily = {"ART-1": {"name": "Товар", "offer_id": "ART-1", "sku": "111", "days": {
    "2026-08-05": {"revenue": 1000, "session_view": 45, "ad_views": 900,
                   "ad_clicks": 45, "hits_view": 300},
    "2026-08-06": {"revenue": 900, "session_view": 20, "hits_view": 250}}}}
cab = {"111": {"2026-08-05": {"views": 12500, "sessions": 3040,
                              "tocart": 621, "position": 7}}}
filled = P.merge_cabinet(daily, cab, {111: {"offer_id": "ART-1"}})
d5 = daily["ART-1"]["days"]["2026-08-05"]
d6 = daily["ART-1"]["days"]["2026-08-06"]
check("показы кабинета вытеснили показы из поиска", d5["hits_view"] == 12500, d5)
check("клики кабинета вытеснили рекламные", d5["session_view"] == 3040, d5)
check("корзина появилась", d5["hits_tocart"] == 621, d5)
check("день без данных в выгрузке не тронут",
      d6["hits_view"] == 250 and d6["session_view"] == 20, d6)
check("сообщено, что именно закрыто",
      filled == {"hits_view", "session_view", "hits_tocart",
                 "position_category"}, filled)
check("товар найден по sku, хотя в daily ключ — артикул", True)

print("\n9. Подписи строк и CTR меняются вместе с источником")
rows_ad = R._quality_rows_for(set())
rows_cab = R._quality_rows_for({"session_view"})
check("без выгрузки клики подписаны как рекламные",
      ("клики (реклама)", "session_view", rows_ad[1][2]) == rows_ad[1], rows_ad[1])
check("с выгрузкой пометка снята", rows_cab[1][0] == "клики", rows_cab[1])
check("и у CTR тоже", rows_cab[2][0] == "CTR", rows_cab[2])

day = {"hits_view": 12500, "session_view": 3040, "ad_views": 900,
       "ad_clicks": 45, "hits_tocart": 621, "revenue": 100000, "ad_spend": 14000}
v_ad = R._quality_day_values(day, unified=False)
v_cab = R._quality_day_values(day, unified=True)
check("без выгрузки CTR по рекламной паре",
      abs(v_ad["ctr"] - 45 / 900) < 1e-9, v_ad["ctr"])
check("с выгрузкой CTR по своей паре",
      abs(v_cab["ctr"] - 3040 / 12500) < 1e-9, v_cab["ctr"])
check("% корзины считается от кликов, как в образце заказчика",
      abs(v_cab["cart_rate"] - 621 / 3040) < 1e-9, v_cab["cart_rate"])

print("\n10. Итог строки CTR сходится со способом расчёта дней")
vals = {"d1": R._quality_day_values(day, True),
        "d2": R._quality_day_values(day, True)}
tot = R._quality_row_total("ctr", vals, ["d1", "d2"], unified=True)
check("итог = сумма кликов / сумма показов",
      abs(tot - (3040 * 2) / (12500 * 2)) < 1e-9, tot)
vals = {"d1": R._quality_day_values(day, False),
        "d2": R._quality_day_values(day, False)}
tot = R._quality_row_total("ctr", vals, ["d1", "d2"], unified=False)
check("без выгрузки итог по рекламной паре",
      abs(tot - (45 * 2) / (900 * 2)) < 1e-9, tot)

print("\n11. Сноски нет, когда выгрузка всё закрыла")
totals = {"d": {"hits_view": 1, "hits_tocart": 1, "cart_rate": 0.2,
                "position_category": 3}}
check("пустых строк не осталось",
      R._quality_empty_keys(totals, ["d"]) == [],
      R._quality_empty_keys(totals, ["d"]))

print("\n12. Нет файла — сбор не падает")
check("пустая папка даёт пустой результат",
      CAB.load_local("НЕТ ТАКОГО", "/tmp/нет-такой-папки") == {})


class CfgNoGoogle:
    DATA_DIR = "/tmp/нет-такой-папки"
    GOOGLE_IMPORT_FOLDER = ""
    GOOGLE_CREDENTIALS_FILE = ""


check("load() без настроек возвращает пусто, а не исключение",
      CAB.load("ТЕСТ", CfgNoGoogle()) == {})

print("\n13. Сноска называет решение, а не только причину")
from openpyxl import Workbook as _WB
_ws = _WB().active
_r = R._quality_write_notes(_ws, 1, ["hits_tocart"], 3)
_texts = [str(_ws.cell(i, 1).value or "") for i in range(1, _r + 1)]
check("сказано про выгрузку из кабинета",
      any("ВЫГРУЗКА_ИЗ_КАБИНЕТА" in t for t in _texts), _texts)
check("и про Premium Plus как второй путь",
      any("Premium Plus" in t for t in _texts), _texts)

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
