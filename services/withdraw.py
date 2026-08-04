"""Отзыв заявки автором.

Автор может отозвать свою заявку, пока её никто не тронул — то есть пока
статус ещё «Новая». После отзыва в реестре стоит «Отозвана», карточки у всех
финансистов закрываются (текст обновляется, кнопки статуса убираются), и
сменить статус такой заявки уже нельзя (проверка — в bot/finance_actions).

Общая точка для обоих каналов: команда /my в чате и Mini App.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode

from bot.models import STATUS_NEW, STATUS_WITHDRAWN
from config import settings
from services import audit, cards, storage
from services.notifier import resolved_finance_ids

log = logging.getLogger(__name__)


async def withdraw_request(
    bot: Bot, request_id: str, *, actor_id: int, actor_name: str
) -> tuple[bool, str]:
    """Отзывает заявку. Возвращает (получилось ли, сообщение автору).

    Чужую заявку отозвать нельзя (проверка по колонке «Telegram ID»), уже
    обработанную — тоже: финансист мог её оплатить, и отзыв ввёл бы в
    заблуждение. Отказы попадают в аудит.
    """
    row = await storage.get_request(request_id)
    if row is None:
        return False, "Заявка не найдена в реестре."

    if row.get("Telegram ID", "") != str(actor_id):
        await audit.log_event(
            audit.WITHDRAW_DENIED, actor_id, actor_name, f"{request_id}: чужая заявка"
        )
        return False, "Отозвать можно только свою заявку."

    status = row.get("Статус оплаты", "")
    if status != STATUS_NEW:
        await audit.log_event(
            audit.WITHDRAW_DENIED, actor_id, actor_name, f"{request_id}: статус «{status}»"
        )
        return False, (
            f"Заявку уже обработали — статус «{status}». Отозвать нельзя, "
            "напишите финансисту."
        )

    if await storage.set_request_status(request_id, STATUS_WITHDRAWN) is None:
        return False, "Не удалось обновить статус заявки. Попробуйте позже."

    now_s = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    status_line = (
        f"{cards.STATUS_MARK} 🚫 Отозвана автором</b> · "
        f"{html.escape(actor_name)} · {now_s}"
    )
    # keyboard=None: кнопки статуса убираем — оплачивать больше нечего.
    await cards.update_all(bot, request_id, status_line, keyboard=None)

    await _notify_finance(bot, row, request_id, actor_name)
    await audit.log_event(audit.REQUEST_WITHDRAWN, actor_id, actor_name, request_id)
    log.info("Заявка %s отозвана автором", request_id)
    return True, "Заявка отозвана — финансисты уведомлены."


async def _notify_finance(
    bot: Bot, row: dict[str, str], request_id: str, actor_name: str
) -> int:
    """Сообщает финансистам об отзыве. Отредактированной карточки мало.

    Карточка могла уйти вниз чата или вообще не отредактироваться (сообщение
    удалено, Telegram отверг правку) — отдельное сообщение гарантирует, что
    финансист не оплатит отозванное. Возвращает число уведомлённых.
    """
    e = html.escape
    text = (
        "🚫 <b>Заявка отозвана автором</b>\n"
        f"№ {e(request_id)}\n"
        f"Контрагент: {e(row.get('Контрагент', '—'))}\n"
        f"Сумма: {e(row.get('Сумма', '—'))} {e(row.get('Валюта', ''))}\n"
        f"Автор: {e(actor_name)}\n\n"
        "Оплачивать не нужно."
    )
    delivered = 0
    for chat_id in resolved_finance_ids():
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception:  # noqa: BLE001 — один недоступный не срывает остальных
            log.warning("Не удалось сообщить финансисту %s об отзыве %s", chat_id, request_id)
    return delivered
