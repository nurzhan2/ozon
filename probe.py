#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Поиск настоящего Client-Id и проверка, жив ли Api-Key аналитики.

Делает две вещи.

1. ДИФФЕРЕНЦИАЛЬНЫЙ ТЕСТ.
   OZON отвечает «Invalid Api-Key» и на неверный ключ, и на неверную пару.
   Отправляем тот же Api-Key с заведомо чужим Client-Id (1). Если ответ
   ОТЛИЧАЕТСЯ от ответа с нашим Client-Id — значит наш Client-Id распознан,
   и проблема в самом ключе. Если ответ ОДИНАКОВЫЙ — OZON не различает
   случаи, и виноват скорее Client-Id.

2. ПОИСК ID В РЕКЛАМНОМ API.
   Рекламные ключи работают, значит через них можно попробовать вытащить
   числовой идентификатор продавца: смотрим ответы доступных эндпоинтов и
   собираем все похожие числа, затем проверяем каждое против Seller API.

Запуск: python probe.py
"""

import re
import sys
import time

import requests

import config

SELLER = "https://api-seller.ozon.ru"
PERF = "https://api-performance.ozon.ru"


def seller_probe(client_id, api_key, timeout=25):
    """Возвращает (status_code, короткое сообщение)."""
    try:
        r = requests.post(
            SELLER + "/v3/product/list",
            headers={"Client-Id": str(client_id), "Api-Key": api_key,
                     "Content-Type": "application/json"},
            json={"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return None, f"нет соединения: {e}"
    try:
        msg = r.json().get("message", r.text[:100])
    except Exception:
        msg = r.text[:100]
    return r.status_code, msg


def perf_token(store, timeout=25):
    r = requests.post(
        PERF + "/api/client/token",
        json={"client_id": store["perf_client_id"],
              "client_secret": store["perf_client_secret"],
              "grant_type": "client_credentials"},
        timeout=timeout,
    )
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


# Эндпоинты рекламного API, в ответах которых может встретиться ID продавца.
PERF_ENDPOINTS = [
    "/api/client/campaign",
    "/api/client/limits",
    "/api/client/statistics/list",
    "/api/client/vendors/statistics/list",
]


def collect_numbers(token, timeout=25):
    """Собирает числа-кандидаты (5-12 цифр) из ответов рекламного API."""
    found = {}
    for path in PERF_ENDPOINTS:
        try:
            r = requests.get(PERF + path,
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=timeout)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        text = r.text[:200000]
        # ключи, в которых обычно лежит идентификатор продавца
        for m in re.finditer(r'"(\w*(?:seller|client|owner|account)\w*Id)"\s*:\s*"?(\d{5,12})"?',
                             text, re.IGNORECASE):
            found.setdefault(m.group(2), set()).add(f"{path}:{m.group(1)}")
    return found


def main():
    print("=" * 72)
    print("ПОИСК CLIENT-ID И ПРОВЕРКА КЛЮЧА АНАЛИТИКИ")
    print("=" * 72)

    solved = {}

    for store in config.STORES:
        name = store["name"]
        api_key = store["api_key"]
        cid = str(store.get("client_id", "")).strip()
        print(f"\n--- {name} ---")

        # 1. дифференциальный тест
        code_ours, msg_ours = seller_probe(cid, api_key)
        time.sleep(0.4)
        code_fake, msg_fake = seller_probe("1", api_key)
        print(f"  наш Client-Id {cid}: HTTP {code_ours} | {msg_ours}")
        print(f"  чужой Client-Id 1  : HTTP {code_fake} | {msg_fake}")
        if (code_ours, msg_ours) == (code_fake, msg_fake):
            print("  -> ответы совпали: OZON не различает случаи, "
                  "скорее всего неверен Client-Id")
        else:
            print("  -> ответы разные: наш Client-Id распознан, "
                  "значит недействителен сам Api-Key")

        # 2. поиск идентификатора в рекламном API
        token = perf_token(store)
        if not token:
            print("  рекламный токен не получен — поиск ID пропущен")
            continue

        nums = collect_numbers(token)
        nums.pop(cid, None)          # исключаем уже проверенное
        if not nums:
            print("  в ответах рекламного API идентификатор продавца не найден")
            continue

        print(f"  найдено кандидатов: {len(nums)} — проверяю против Seller API")
        for num, where in list(nums.items())[:8]:
            code, msg = seller_probe(num, api_key)
            mark = "OK" if code == 200 else "нет"
            print(f"    {num} ({', '.join(sorted(where))}): HTTP {code} {mark}")
            if code == 200:
                solved[name] = num
                break
            time.sleep(0.4)

    print("\n" + "=" * 72)
    if solved:
        print("НАЙДЕНЫ РАБОЧИЕ CLIENT-ID:")
        for k, v in solved.items():
            print(f"    {k}: {v}")
    else:
        print("Рабочий Client-Id найти не удалось.")
        print("Нужно запросить у заказчика Client-Id и Api-Key с одной страницы")
        print("кабинета: Настройки -> Seller API.")
    print()
    return 0 if solved else 1


if __name__ == "__main__":
    sys.exit(main())
