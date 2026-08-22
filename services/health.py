"""Пульс Telegram-подключения: жив ли контур «бот → api.telegram.org».

Фоновая задача probe_loop раз в PROBE_INTERVAL дёргает getMe и записывает
время последнего успеха; /api/health отдаёт 503, когда успеха не было дольше
STALE_AFTER. Внешний мониторинг (UptimeRobot и т.п.) по коду ответа различает:
таймаут/502 — умер контейнер или сервер, 503 — процесс жив, но Telegram
недоступен (обычно мёртв прокси). См. DEPLOY.md, раздел «Внешний мониторинг».

Внешнего мониторинга может и не быть, поэтому пульс сам рассказывает админам
о провалах: короткие моргания прокси не тревожат никого (порог настраивается
в панели), а о заметном провале приходит сообщение — и почти всегда уже после
восстановления, вместе с длительностью. Пока связи нет, Telegram не доставит
ничего: сообщение о сбое ушло бы тем же мёртвым каналом.
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

from services import alerts
from services import runtime_settings as rs

log = logging.getLogger(__name__)

PROBE_INTERVAL = 60.0
# Сколько секунд без успешного вызова Telegram API считаем нездоровьем:
# больше двух интервалов пульса, чтобы одиночный сбой не давал ложный алерт.
STALE_AFTER = 180.0

_started = time.monotonic()
_last_ok: float | None = None
# Начало текущего провала (monotonic) и признак «о нём уже пробовали сказать».
_down_since: float | None = None
_down_reported = False


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


def down_for() -> float | None:
    """Сколько секунд длится текущий провал (None — связь есть)."""
    return None if _down_since is None else time.monotonic() - _down_since


def link_state() -> dict:
    """Состояние контура для админ-панели.

    Панель живёт на нашем домене и открывается, даже когда бот до Telegram не
    достаёт, — значит это единственное место, где админ может увидеть провал,
    пока он идёт.
    """
    age = last_ok_age()
    down = down_for()
    return {
        "alive": telegram_alive(),
        "last_ok_age": None if age is None else int(age),
        "down_for": None if down is None else int(down),
        "grace_min": rs.alerts_config()["link_grace_min"],
    }


def _grace_seconds() -> float:
    try:
        return rs.alerts_config()["link_grace_min"] * 60.0
    except Exception:  # noqa: BLE001 — настройки не должны валить пульс
        log.exception("Не удалось прочитать порог уведомления о связи")
        return rs.LINK_GRACE_DEFAULT * 60.0


def _minutes(seconds: float) -> int:
    """Минуты к ближайшему, но не меньше одной: «лежала 0 мин» — не строка."""
    return max(1, int(seconds / 60 + 0.5))


async def probe_once(bot: Bot, healthy: bool) -> bool:
    """Один такт пульса. Возвращает состояние контура после проверки.

    Вынесен из цикла ради тестов: уведомления о провале нужно проверять,
    не гоняя настоящие паузы.
    """
    global _down_since, _down_reported
    try:
        await bot.get_me()
    except Exception as exc:  # noqa: BLE001 — сеть/прокси; пульс не падает
        if healthy:
            log.warning("Telegram API недоступен: %s", exc)
            _down_since = time.monotonic()
            _down_reported = False
        down = down_for() or 0.0
        if not _down_reported and down >= _grace_seconds():
            # Единственная попытка на провал: повторять бессмысленно (канал
            # тот же самый), а журнал инцидентов забился бы счётчиком.
            _down_reported = True
            await _safe_alert(
                bot,
                "Связь с Telegram пропала",
                f"Нет ответа от api.telegram.org {_minutes(down)} мин. "
                "Обычно это прокси: проверьте контейнер warp.",
                "tg-link-down",
            )
        return False

    record_ok()
    if not healthy:
        log.info("Telegram API снова доступен")
        down = down_for() or 0.0
        if down >= _grace_seconds():
            await _safe_alert(
                bot,
                "Связь с Telegram восстановлена",
                f"Провал длился {_minutes(down)} мин. Заявки, поданные в это "
                "время, карточку финансисту могли не отдать — проверьте реестр.",
                "tg-link-up",
            )
    _down_since = None
    _down_reported = False
    return True


async def _safe_alert(bot: Bot, title: str, details: str, signature: str) -> None:
    """Уведомление о связи. Сбой отправки — норма: связи-то и нет."""
    try:
        await alerts.alert_admins(
            bot, title, details, signature=signature, kind="telegram"
        )
    except Exception:  # noqa: BLE001 — пульс важнее уведомления о пульсе
        log.warning("Не удалось отправить уведомление о состоянии связи")


async def probe_loop(bot: Bot) -> None:
    """Фоновый пульс: getMe раз в PROBE_INTERVAL, лог — только на переходах."""
    healthy = True
    while True:
        healthy = await probe_once(bot, healthy)
        await asyncio.sleep(PROBE_INTERVAL)
