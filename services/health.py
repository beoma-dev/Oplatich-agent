"""Пульс Telegram-подключения: жив ли контур «бот → api.telegram.org».

Фоновая задача probe_loop раз в PROBE_INTERVAL дёргает getMe и записывает
время последнего успеха; /api/health отдаёт 503, когда успеха не было дольше
STALE_AFTER. Внешний мониторинг (UptimeRobot и т.п.) по коду ответа различает:
таймаут/502 — умер контейнер или сервер, 503 — процесс жив, но Telegram
недоступен (обычно мёртв прокси). См. DEPLOY.md, раздел «Внешний мониторинг».
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

log = logging.getLogger(__name__)

PROBE_INTERVAL = 60.0
# Сколько секунд без успешного вызова Telegram API считаем нездоровьем:
# больше двух интервалов пульса, чтобы одиночный сбой не давал ложный алерт.
STALE_AFTER = 180.0

_started = time.monotonic()
_last_ok: float | None = None


def record_ok() -> None:
    """Отмечает успешный вызов Telegram API."""
    global _last_ok
    _last_ok = time.monotonic()


def last_ok_age() -> float | None:
    """Секунд с последнего успешного вызова (None — успехов ещё не было)."""
    return None if _last_ok is None else time.monotonic() - _last_ok


def telegram_alive() -> bool:
    """True, пока Telegram отвечал недавно.

    До первого успешного пульса действует грейс на время старта процесса —
    иначе healthcheck мигал бы 503 в первые секунды после запуска.
    """
    age = last_ok_age()
    if age is None:
        return time.monotonic() - _started < STALE_AFTER
    return age < STALE_AFTER


async def probe_loop(bot: Bot) -> None:
    """Фоновый пульс: getMe раз в PROBE_INTERVAL, лог — только на переходах."""
    healthy = True
    while True:
        try:
            await bot.get_me()
            record_ok()
            if not healthy:
                log.info("Telegram API снова доступен")
            healthy = True
        except Exception as exc:  # noqa: BLE001 — сеть/прокси; пульс не падает
            if healthy:
                log.warning("Telegram API недоступен: %s", exc)
            healthy = False
        await asyncio.sleep(PROBE_INTERVAL)
