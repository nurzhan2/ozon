#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Постоянный процесс для Railway: сам держит расписание отчётов.

  * 08:00 по Москве          — утренний пакет (отчёты 1–4)
  * каждые 2 часа с 8 до 22  — промежуточный отчёт (отчёт 5)

Почему один процесс, а не два cron-сервиса Railway: снимки для внутридневного
сравнения должны лежать на одном диске, а расписаний нужно два. Один воркер
с планировщиком внутри решает и то, и другое, и его проще наблюдать в логах.

При старте сервис сразу проверяет доступы и печатает план запусков, поэтому по
логу видно, что он жив и когда сработает в следующий раз.
"""

import os
import sys
import time
import logging
import traceback
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import config
from ozon.collector import StoreCollector
from ozon import reports
import run as runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

TZ = ZoneInfo(config.TIMEZONE) if ZoneInfo else None

MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "8"))
INTRADAY_HOURS = [int(h) for h in
                  os.environ.get("INTRADAY_HOURS", "8,10,12,14,16,18,20,22").split(",")
                  if h.strip()]


def now():
    return datetime.now(TZ) if TZ else datetime.now()


def build_collectors():
    return [
        StoreCollector(st,
                       enable_performance=config.ENABLE_PERFORMANCE,
                       exclude_marker=config.EXCLUDE_ARTICLE_MARKER)
        for st in config.STORES
    ]


def run_job(kind):
    """kind: 'morning' | 'intraday'. Ошибка одного отчёта не роняет остальные."""
    started = now()
    log.info("=" * 60)
    log.info("ЗАПУСК: %s (%s)", kind, started.strftime("%Y-%m-%d %H:%M"))
    made, failed = [], []
    try:
        cols = build_collectors()
    except Exception as e:
        log.error("Не удалось подготовить магазины: %s", e)
        return

    keys = runner.DAILY if kind == "morning" else ("intraday",)
    for key in keys:
        try:
            path = runner.COMMANDS[key](cols, config)
            made.append((key, path))
            log.info("готово: %s -> %s", key, path)
        except Exception as e:
            failed.append(key)
            log.error("отчёт «%s» не собран: %s", key, e)
            log.debug(traceback.format_exc())

    if made and config.UPLOAD_TO_GOOGLE:
        try:
            links = runner.upload_all(made)
            for title, url in links:
                log.info("в Google Таблицах: %s -> %s", title, url)
        except Exception as e:
            log.error("выгрузка в Google не удалась: %s", e)

    took = (now() - started).total_seconds()
    log.info("ИТОГ %s: собрано %d, с ошибкой %d, заняло %.0f сек",
             kind, len(made), len(failed), took)


def next_run_after(dt):
    """Ближайший момент запуска после dt и его тип."""
    candidates = []
    for day_shift in (0, 1):
        base = (dt + timedelta(days=day_shift)).replace(minute=0, second=0, microsecond=0)
        for h in sorted(set(INTRADAY_HOURS + [MORNING_HOUR])):
            moment = base.replace(hour=h)
            if moment > dt:
                kinds = []
                if h == MORNING_HOUR:
                    kinds.append("morning")
                if h in INTRADAY_HOURS:
                    kinds.append("intraday")
                candidates.append((moment, kinds))
    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def startup_check():
    log.info("Часовой пояс: %s, сейчас %s", config.TIMEZONE, now().strftime("%Y-%m-%d %H:%M"))
    log.info("Магазинов в конфигурации: %d (%s)",
             len(config.STORES), ", ".join(s["name"] for s in config.STORES))
    log.info("Утренний пакет в %02d:00; промежуточный в %s",
             MORNING_HOUR, ", ".join(f"{h:02d}:00" for h in INTRADAY_HOURS))
    log.info("Данные: %s | выгрузка в Google: %s",
             config.DATA_DIR, "включена" if config.UPLOAD_TO_GOOGLE else "выключена")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config.SNAPSHOTS_DIR, exist_ok=True)


def main():
    startup_check()

    # Разовый запуск при старте — удобно, чтобы сразу увидеть результат
    # после деплоя, не дожидаясь 8 утра.
    if os.environ.get("RUN_ON_START", "").strip().lower() in ("1", "true", "yes"):
        kind = os.environ.get("RUN_ON_START_KIND", "morning")
        log.info("RUN_ON_START включён — выполняю «%s» сразу", kind)
        run_job(kind)

    while True:
        moment, kinds = next_run_after(now())
        wait = (moment - now()).total_seconds()
        log.info("Следующий запуск: %s (%s), ждать %.0f мин",
                 moment.strftime("%Y-%m-%d %H:%M"), "+".join(kinds), wait / 60)
        # спим частями, чтобы переживать сдвиги времени и корректно логировать
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (moment - now()).total_seconds()

        if "morning" in kinds:
            run_job("morning")
        if "intraday" in kinds:
            run_job("intraday")
        time.sleep(60)   # чтобы не сработать дважды в тот же час


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено вручную")
