"""Действия финансиста: смена статуса заявки кнопками на карточке.

callback_data: ST:<request_id>:<KEY> (см. services/notifier.build_status_keyboard).

Правила:
  «✅ Оплачено» — применяется сразу;
  «⏸ Отложено» и «❌ Отклонено» — бот просит ПРИЧИНУ (следующим сообщением
  финансиста или кнопкой «Без причины»).

При смене статуса обновляются карточки у ВСЕХ финансистов (services/cards),
статус пишется в реестр, автор получает уведомление с причиной, событие
фиксируется в аудите.
"""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.models import REQUEST_ID_RE, REQUEST_STATUSES, STATUS_WITHDRAWN
from services import audit
from services import cards as cards_store
from services.notifier import resolved_finance_ids
from services.status_change import apply_status
from services.storage import get_request

log = logging.getLogger(__name__)

# Маркер строки статуса в карточке (общий с services/cards).
_STATUS_MARK = cards_store.STATUS_MARK

# Статусы, требующие причину.
_REASON_KEYS = {"REJECTED", "DEFERRED"}
CB_REASON_SKIP = "RSN_SKIP"

# Ожидание причины: id финансиста → {request_id, key, fallback_card}.
_pending_reasons: dict[int, dict] = {}


def _who(user: User) -> str:
    return f"@{user.username}" if user.username else user.full_name


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие кнопки статуса на карточке заявки."""
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, request_id, key = parts

    if not REQUEST_ID_RE.fullmatch(request_id):
        await query.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    entry = REQUEST_STATUSES.get(key)
    if entry is None:
        await query.answer("Неизвестный статус.", show_alert=True)
        return
    label, status_text = entry

    # Кнопки живут в личке финансиста, но на всякий случай сверяем явно.
    user = update.effective_user
    if user is None or user.id not in resolved_finance_ids():
        if user is not None:
            await audit.log_event(
                audit.STATUS_DENIED, user.id, _who(user), f"{request_id} → {key}"
            )
        await query.answer("Менять статус заявок могут только финансисты.", show_alert=True)
        return

    # Отозванную автором заявку финансист не оплачивает: карточка могла
    # остаться открытой в чужом чате, если её не удалось отредактировать.
    current = await get_request(request_id)
    if current is not None and current.get("Статус оплаты") == STATUS_WITHDRAWN:
        await query.answer(
            "Заявка отозвана автором — статус менять нельзя.", show_alert=True
        )
        return

    # Карточка, на которой нажали, — резерв на случай старых заявок,
    # разосланных до появления таблицы карточек.
    fallback_card = None
    message = query.message
    if message is not None:
        base_html = message.caption_html if message.caption is not None else message.text_html
        fallback_card = {
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "is_caption": message.caption is not None,
            "base_html": (base_html or "").split(_STATUS_MARK)[0],
        }

    if key in _REASON_KEYS:
        _pending_reasons[user.id] = {
            "request_id": request_id,
            "key": key,
            "fallback_card": fallback_card,
        }
        await query.answer()
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"✍️ Укажите причину для «{label}» по заявке "
                f"<code>{html.escape(request_id)}</code> — одним сообщением.\n"
                "Или отправьте без пояснения:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➡️ Без причины", callback_data=CB_REASON_SKIP)]]
            ),
        )
        return

    applied, message = await apply_status(
        context.bot,
        request_id,
        key,
        actor_id=user.id,
        actor_name=_who(user),
        fallback_card=fallback_card,
    )
    await query.answer(message, show_alert=not applied)


async def reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст финансиста после запроса причины (группа -2, до прочих хендлеров)."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    pending = _pending_reasons.pop(user.id, None)
    if pending is None:
        return  # не наш случай — апдейт пойдёт дальше (форма, fallback и т.п.)

    reason = (message.text or "").strip()[:300]
    applied, _ = await apply_status(
        context.bot,
        pending["request_id"],
        pending["key"],
        actor_id=user.id,
        actor_name=_who(user),
        reason=reason or None,
        fallback_card=pending["fallback_card"],
    )
    label = REQUEST_STATUSES[pending["key"]][0]
    await message.reply_text(
        f"Готово: {label}, причина передана автору." if applied
        else "⚠️ Заявка не найдена в реестре."
    )
    raise ApplicationHandlerStop


async def reason_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Без причины» под запросом причины."""
    query = update.callback_query
    user = update.effective_user
    pending = _pending_reasons.pop(user.id, None) if user else None
    if pending is None:
        await query.answer("Запрос устарел.", show_alert=True)
        return
    await query.answer()
    applied, _ = await apply_status(
        context.bot,
        pending["request_id"],
        pending["key"],
        actor_id=user.id,
        actor_name=_who(user),
        fallback_card=pending["fallback_card"],
    )
    label = REQUEST_STATUSES[pending["key"]][0]
    await query.edit_message_text(
        f"Готово: {label} (без причины)." if applied else "⚠️ Заявка не найдена в реестре."
    )
