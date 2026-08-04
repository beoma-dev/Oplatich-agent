"""Регламент плановой даты оплаты. Считается ТОЛЬКО на сервере, в TIMEZONE.

Правила:
  срочная  → сегодня (в таймзоне приложения, не браузера);
  обычная  → следующий рабочий день: пятница и выходные переносятся
             на понедельник.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings


def app_today() -> date:
    """Сегодня в таймзоне приложения (TIMEZONE), а не сервера/клиента."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def next_business_day(day: date) -> date:
    """Следующий рабочий день после указанного (сб/вс пропускаются)."""
    result = day + timedelta(days=1)
    while result.weekday() >= 5:  # 5 = суббота, 6 = воскресенье
        result += timedelta(days=1)
    return result


def auto_planned_date(urgent: bool, today: date | None = None) -> date:
    """Плановая дата по срочности: срочно → сегодня, иначе следующий рабочий."""
    today = today or app_today()
    return today if urgent else next_business_day(today)
