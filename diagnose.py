#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика доступов OZON — запускается на сервере, где есть сеть до OZON.

Отвечает на три вопроса по каждому магазину:
  1. Жив ли Api-Key аналитики вообще (Seller API).
  2. Живы ли рекламные ключи (Performance API) — если да, аккаунт существует,
     и проблема только в паре Client-Id + Api-Key.
  3. Какой Client-Id подходит: проверяются осмысленные кандидаты
     (число из рекламного ID, полный рекламный ID).

Зачем: OZON на любую проблему с парой ключей отвечает одинаково —
«Invalid Api-Key», не различая неверный ключ и неверный Client-Id.
Скрипт разделяет эти случаи, чтобы точно знать, что просить у заказчика.

Запуск: python diagnose.py
"""

import sys
import time

import requests

import config

SELLER = "https://api-seller.ozon.ru"
PERF = "https://api-performance.ozon.ru"


def try_seller(client_id, api_key, timeout=25):
    """Пробует самый дешёвый метод Seller API. Возвращает (ок, пояснение)."""
    try:
        r = requests.post(
            SELLER + "/v3/product/list",
            headers={"Client-Id": str(client_id), "Api-Key": api_key,
                     "Content-Type": "application/json"},
            json={"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return False, f"нет соединения: {e}"

    if r.status_code == 200:
        data = r.json()
        total = (data.get("result") or {}).get("total", "?")
        return True, f"OK, товаров в кабинете: {total}"
    try:
        msg = r.json().get("message", r.text[:120])
    except Exception:
        msg = r.text[:120]
    return False, f"HTTP {r.status_code}: {msg}"


def try_performance(perf_id, perf_secret, timeout=25):
    """Проверяет рекламные ключи. Возвращает (ок, пояснение)."""
    if not perf_id or not perf_secret:
        return False, "рекламные ключи не заданы"
    try:
        r = requests.post(
            PERF + "/api/client/token",
            json={"client_id": perf_id, "client_secret": perf_secret,
                  "grant_type": "client_credentials"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return False, f"нет соединения: {e}"

    if r.status_code != 200:
        return False, f"токен не получен, HTTP {r.status_code}: {r.text[:120]}"

    token = r.json().get("access_token")
    try:
        c = requests.get(PERF + "/api/client/campaign",
                         headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        if c.status_code == 200:
            data = c.json()
            camps = data.get("list") or data.get("campaigns") or []
            return True, f"OK, токен получен, кампаний: {len(camps)}"
        return True, f"токен получен, но кампании недоступны (HTTP {c.status_code})"
    except requests.RequestException as e:
        return True, f"токен получен, кампании не проверены: {e}"


def candidates(store):
    """Осмысленные варианты Client-Id для проверки — без слепого перебора."""
    out = []
    cid = str(store.get("client_id", "")).strip()
    if cid:
        out.append((cid, "из конфигурации"))
    perf = str(store.get("perf_client_id", "")).strip()
    if perf:
        left = perf.split("@")[0]
        num = left.split("-")[0]
        if num and num != cid:
            out.append((num, "число из рекламного ID"))
        if left != num and left != cid:
            out.append((left, "рекламный ID без домена"))
    return out


def main():
    print("=" * 70)
    print("ДИАГНОСТИКА ДОСТУПОВ OZON")
    print("=" * 70)

    verdicts = []
    for store in config.STORES:
        name = store["name"]
        print(f"\n--- {name} ---")

        # 1. Рекламные ключи: жив ли аккаунт вообще
        ok_perf, msg_perf = try_performance(store.get("perf_client_id"),
                                            store.get("perf_client_secret"))
        print(f"  Performance API (реклама): {'OK   ' if ok_perf else 'ОШИБКА'} {msg_perf}")

        # 2. Seller API с разными кандидатами Client-Id
        seller_ok = None
        for cid, origin in candidates(store):
            ok, msg = try_seller(cid, store["api_key"])
            print(f"  Seller API, Client-Id {cid} ({origin}): "
                  f"{'OK   ' if ok else 'ОШИБКА'} {msg}")
            if ok:
                seller_ok = cid
                break
            time.sleep(0.5)

        verdicts.append({
            "store": name,
            "perf_ok": ok_perf,
            "seller_client_id": seller_ok,
        })

    # ------------------------------------------------------------- вывод
    print("\n" + "=" * 70)
    print("ВЫВОД")
    print("=" * 70)

    works = [v for v in verdicts if v["seller_client_id"]]
    perf_alive = [v for v in verdicts if v["perf_ok"]]

    if works:
        print("Подошедшие Client-Id — впишите их в OZON_STORES:")
        for v in works:
            print(f"    {v['store']}: {v['seller_client_id']}")
    else:
        print("Ни один вариант Client-Id не подошёл к Seller API.")

    if perf_alive and not works:
        print()
        print(f"При этом рекламные ключи РАБОТАЮТ у {len(perf_alive)} из {len(verdicts)} "
              f"магазинов. Значит кабинеты существуют и доступ к ним есть, "
              f"а проблема именно в паре Client-Id + Api-Key для Seller API:")
        print("  - либо Api-Key аналитики отозван или пересоздан,")
        print("  - либо нужен настоящий числовой Client-Id из кабинета")
        print("    (Настройки -> Seller API, рядом с ключом).")
    elif not perf_alive and not works:
        print()
        print("Не работают ни аналитика, ни реклама. Похоже, ключи отозваны "
              "целиком — нужно перевыпустить их в кабинете.")

    print()
    return 0 if works else 1


if __name__ == "__main__":
    sys.exit(main())
