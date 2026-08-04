# -*- coding: utf-8 -*-
"""Помощники по датам (в часовом поясе OZON / МСК)."""

from datetime import datetime, timedelta, date

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None


def now_tz(tz_name="Europe/Moscow"):
    if ZoneInfo:
        return datetime.now(ZoneInfo(tz_name))
    return datetime.now()


def d(x):
    """date -> 'YYYY-MM-DD'."""
    return x.strftime("%Y-%m-%d")


def today(tz_name="Europe/Moscow"):
    return now_tz(tz_name).date()


def yesterday(tz_name="Europe/Moscow"):
    return today(tz_name) - timedelta(days=1)


def day_before_yesterday(tz_name="Europe/Moscow"):
    return today(tz_name) - timedelta(days=2)


def month_start(ref=None, tz_name="Europe/Moscow"):
    ref = ref or today(tz_name)
    return date(ref.year, ref.month, 1)
