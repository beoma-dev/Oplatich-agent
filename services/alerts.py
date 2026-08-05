"""Алерты админам о сбоях: «тихих» падений быть не должно.

Отправляются в личку всем из ADMIN_IDS. Троттлинг двухуровневый:
одна и та же ошибка (по сигнатуре) — не чаще раза в 30 минут, суммарно —
не более 10 алертов в час: шторм однотипных ошибок не превращается в спам.

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

from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Одинаковая ошибка — не чаще раза в этот интервал (сек).
SAME_SIGNATURE_WINDOW = 1800.0
# Общий потолок: не больше GLOBAL_MAX алертов за GLOBAL_WINDOW.
GLOBAL_WINDOW = 3600.0
GLOBAL_MAX = 10

_last_by_signature: dict[str, float] = {}
_sent_times: list[float] = []


def _allowed(signature: str, now: float | None = None) -> bool:
    """Пропускает алерт через троттлинг (и учитывает его)."""
    global _sent_times
    now = time.monotonic() if now is None else now

    _sent_times = [t for t in _sent_times if now - t < GLOBAL_WINDOW]
    if len(_sent_times) >= GLOBAL_MAX:
        return False

    last = _last_by_signature.get(signature)
    if last is not None and now - last < SAME_SIGNATURE_WINDOW:
        return False

    _last_by_signature[signature] = now
    _sent_times.append(now)
    return True


async def alert_admins(
    bot: Bot, title: str, details: str = "", *, signature: str | None = None
) -> int:
    """Шлёт алерт всем админам. Возвращает число доставленных сообщений."""
    if not effective_admin_ids():
        log.warning("Алерт «%s» не отправлен: ADMIN_IDS пуст", title)
        return 0

    sig = signature or hashlib.sha256(f"{title}|{details[:100]}".encode()).hexdigest()[:16]
    if not _allowed(sig):
        return 0

    e = html.escape
    text = f"🚨 <b>{e(title)}</b>"
    if details:
        text += f"\n<code>{e(details[:600])}</code>"

    delivered = 0
    for admin_id in effective_admin_ids():
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception:  # noqa: BLE001 — алерт не должен ломать сценарий
            log.warning("Не удалось доставить алерт админу %s", admin_id)
    return delivered


async def alert_error(bot: Bot, error: BaseException, where: str = "") -> None:
    """Алерт о необработанной ошибке (сигнатура — тип + начало сообщения)."""
    details = f"{where}\n{error}" if where else str(error)
    signature = f"{type(error).__name__}:{str(error)[:80]}"
    try:
        await alert_admins(
            bot, f"Ошибка бота: {type(error).__name__}", details, signature=signature
        )
    except Exception:  # noqa: BLE001
        log.exception("Сбой при отправке алерта об ошибке")
