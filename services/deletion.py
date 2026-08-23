"""Удаление заявки из реестра.

Удаление необратимо и стирает финансовую запись, поэтому:
  • администратор бота удаляет любую заявку;
  • автор — только свою и только уже отозванную: пока заявка «Новая», её
    сначала отзывают (финансисты получают уведомление), а «Оплаченную»
    удаляет только админ, осознанно;
  • каждое удаление и каждый отказ попадают в аудит-журнал;
  • карточки у финансистов закрываются пометкой «удалена».
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

from bot.models import STATUS_WITHDRAWN
from config import settings
from services import audit, cards, storage

log = logging.getLogger(__name__)


async def delete_request(
    bot: Bot, request_id: str, *, actor_id: int, actor_name: str, is_admin: bool
) -> tuple[bool, str]:
    """Удаляет заявку. Возвращает (получилось ли, сообщение пользователю)."""
    row = await storage.get_request(request_id)
    if row is None:
        return False, "Заявка не найдена в реестре."

    status = row.get("Статус оплаты", "")
    if not is_admin:
        if row.get("Telegram ID", "") != str(actor_id):
            await audit.log_event(
                audit.DELETE_DENIED, actor_id, actor_name, f"{request_id}: чужая заявка"
            )
            return False, "Удалить можно только свою заявку."
        if status != STATUS_WITHDRAWN:
            await audit.log_event(
                audit.DELETE_DENIED, actor_id, actor_name,
                f"{request_id}: статус «{status}»",
            )
            return False, (
                f"Заявку со статусом «{status}» удаляет только администратор. "
                "Сначала отзовите её."
            )

    if not await storage.delete_request(request_id):
        return False, "Не удалось удалить заявку. Попробуйте позже."

    now_s = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    status_line = (
        f"{cards.STATUS_MARK} 🗑 Удалена из реестра</b> · "
        f"{html.escape(actor_name)} · {now_s}"
    )
    await cards.update_all(bot, request_id, status_line, keyboard=None)
    # Сообщения переписаны на «удалена», обновлять больше нечего — адреса
    # карточек можно забыть, иначе они копятся навсегда.
    await cards.delete_for_request(request_id)

    await audit.log_event(
        audit.REQUEST_DELETED, actor_id, actor_name, f"{request_id} · был статус «{status}»"
    )
    log.info("Заявка %s удалена из реестра", request_id)
    return True, "Заявка удалена из реестра."
