"""Алерты админам о сбоях: «тихих» падений быть не должно.

Отправляются в личку всем из ADMIN_IDS. Троттлинг двухуровневый:
одна и та же ошибка (по сигнатуре) — не чаще раза в 30 минут, суммарно —
не более 10 алертов в час: шторм однотипных ошибок не превращается в спам.

Категория (kind) сверяется с настройками из админ-панели: админ решает, о чём
его будить, — кроме критичного (потеря заявки), которое не выключается.
В журнал инцидентов сбой попадает ВСЕГДА, даже выключенный: выключен звонок,
а не датчик, и панель показывает то, что было на самом деле.

Модуль обязан быть безопасным: сбой отправки алерта никогда не влияет
на основной сценарий.
"""
from __future__ import annotations

import hashlib
import html
import logging
import time

from telegram import Bot
from telegram.constants import ParseMode

from services import runtime_settings as rs
from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Одинаковая ошибка — не чаще раза в этот интервал (сек).
SAME_SIGNATURE_WINDOW = 1800.0
# Общий потолок: не больше GLOBAL_MAX алертов за GLOBAL_WINDOW.
GLOBAL_WINDOW = 3600.0
GLOBAL_MAX = 10

_last_by_signature: dict[str, float] = {}
_sent_times: list[float] = []


def _allowed(signature: str, now: float | None = None, *, critical: bool = False) -> bool:
    """Пропускает алерт через троттлинг (и учитывает его).

    critical=True обходит ОБЩИЙ потолок, но не окно по сигнатуре. Иначе
    потолок можно было выесть чем угодно — например, десятком разных ошибок
    из браузера, ручка для которых открыта любому с подписью Telegram, — и
    следующее «заявка НЕ сохранилась» не ушло бы никому целый час. Защита от
    шторма не должна становиться способом заглушить главное.
    """
    global _sent_times
    now = time.monotonic() if now is None else now

    _sent_times = [t for t in _sent_times if now - t < GLOBAL_WINDOW]
    if not critical and len(_sent_times) >= GLOBAL_MAX:
        return False

    last = _last_by_signature.get(signature)
    if last is not None and now - last < SAME_SIGNATURE_WINDOW:
        return False

    _last_by_signature[signature] = now
    _sent_times.append(now)
    return True


def _journal(kind: str | None, title: str, *, sent: bool) -> None:
    """Отметка в журнале инцидентов. Сбой записи не мешает самому алерту."""
    try:
        rs.record_incident(kind, title, sent=sent, when=time.time())
    except Exception:  # noqa: BLE001 — журнал вторичен по отношению к алерту
        log.exception("Не удалось записать инцидент «%s» в журнал", title)


async def _deliver(bot: Bot, text: str, targets: list[int]) -> int:
    delivered = 0
    for chat_id in targets:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception:  # noqa: BLE001 — алерт не должен ломать сценарий
            log.warning("Не удалось доставить алерт админу %s", chat_id)
    return delivered


def _format(title: str, details: str = "", *, icon: str = "🚨") -> str:
    e = html.escape
    text = f"{icon} <b>{e(title)}</b>"
    if details:
        text += f"\n<code>{e(details[:600])}</code>"
    return text


async def alert_admins(
    bot: Bot,
    title: str,
    details: str = "",
    *,
    signature: str | None = None,
    kind: str | None = None,
) -> int:
    """Шлёт алерт всем админам. Возвращает число доставленных сообщений.

    kind — категория из ALERT_KINDS; выключенная в панели не отправляется, но
    в журнал инцидентов попадает всё равно.
    """
    admins = effective_admin_ids()
    if not admins:
        log.warning("Алерт «%s» не отправлен: ADMIN_IDS пуст", title)
        _journal(kind, title, sent=False)
        return 0

    if not rs.alert_kind_enabled(kind):
        log.info("Алерт «%s»: категория %s выключена в панели", title, kind)
        _journal(kind, title, sent=False)
        return 0

    sig = signature or hashlib.sha256(f"{title}|{details[:100]}".encode()).hexdigest()[:16]
    if not _allowed(sig, critical=kind in rs.CRITICAL_ALERT_KEYS):
        _journal(kind, title, sent=False)
        return 0

    delivered = await _deliver(bot, _format(title, details), admins)
    _journal(kind, title, sent=bool(delivered))
    return delivered


async def send_test_alert(bot: Bot, chat_id: int) -> bool:
    """Проверочное уведомление — только тому, кто нажал кнопку.

    Обходит и категории, и троттлинг: это осознанная проверка канала
    «бот → админ», и она обязана срабатывать с первого раза. В журнал
    инцидентов не пишется — сбоя не было.
    """
    text = _format(
        "Проверка уведомлений",
        "Канал работает: так будет выглядеть сообщение о сбое.",
        icon="✅",
    )
    return bool(await _deliver(bot, text, [chat_id]))


async def alert_error(bot: Bot, error: BaseException, where: str = "") -> None:
    """Алерт о необработанной ошибке (сигнатура — тип + начало сообщения)."""
    details = f"{where}\n{error}" if where else str(error)
    signature = f"{type(error).__name__}:{str(error)[:80]}"
    try:
        await alert_admins(
            bot,
            f"Ошибка бота: {type(error).__name__}",
            details,
            signature=signature,
            kind="error",
        )
    except Exception:  # noqa: BLE001
        log.exception("Сбой при отправке алерта об ошибке")
