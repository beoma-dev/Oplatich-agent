"""Хендлеры запроса доступа: кнопка у сотрудника, решение у админа."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.access import is_bot_admin
from services import audit
from services.access_requests import (
    CB_APPROVE,
    CB_ASK,
    request_access,
    resolve_access,
)

log = logging.getLogger(__name__)


def ask_access_markup() -> InlineKeyboardMarkup:
    """Кнопка «попросить доступ» — её показываем вместе с отказом."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✋ Запросить доступ", callback_data=CB_ASK)]]
    )


async def ask_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сотрудник нажал «Запросить доступ»."""
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    if user is None:
        return
    text = await request_access(
        context.bot, user.id, user.username or "", user.full_name or ""
    )
    # Кнопку убираем: повторные нажатия всё равно ничего не добавят.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — сообщение могли удалить
        pass
    await query.message.reply_text(text)


async def resolve_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ нажал «Открыть доступ» или «Отказать» в карточке заявки."""
    query = update.callback_query
    actor = update.effective_user
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
        await query.answer("Не понимаю кнопку.", show_alert=True)
        return
    target_id = int(parts[2])

    if actor is None or not await is_bot_admin(context.bot, actor.id):
        await query.answer("Только для админов бота.", show_alert=True)
        await audit.log_event(
            audit.ADMIN_DENIED,
            actor.id if actor else None,
            f"@{actor.username}" if actor and actor.username else None,
            "решение по доступу",
        )
        return

    await query.answer()
    approve = data.startswith(CB_APPROVE)
    note = await resolve_access(
        context.bot,
        target_id,
        approve,
        actor_id=actor.id,
        actor_name=f"@{actor.username}" if actor.username else actor.full_name,
    )
    # Карточка остаётся в чате как след решения — меняем только подпись.
    try:
        await query.edit_message_text(
            (query.message.text_html or "") + f"\n\n<b>{note}</b>",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001 — не критично, решение уже применено
        await query.message.reply_text(note)
