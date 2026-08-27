"""«Мои заявки»: автор видит статусы своих заявок и управляет ими.

Команда /my показывает последние заявки автора со статусом, суммой, плановой
датой и причиной отказа/отсрочки. Оттуда же:
  🚫 Отозвать — пока заявку не тронули (статус «Новая»).

Кнопка «↻ Повторить» убрана 27.08.2026: в Mini App ряд не вмещал закрывающие
документы, а повтор дублировал обычную подачу. Обработчик оставлен — в уже
разосланных сообщениях кнопки есть, и нажатие в них должно работать.

Список приватный: в группе бот его не печатает, а зовёт в личку — иначе суммы
и контрагенты утекли бы в общий чат.
"""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from bot.access import access_denied_message, is_allowed
from bot.models import STATUS_NEW
from bot.validators import ValidationError, parse_amount
from config import settings
from services import request_meta, storage
from services.withdraw import withdraw_request

log = logging.getLogger(__name__)

# callback_data: MY:WDR:<request_id> / MY:RPT:<request_id>
CB_WITHDRAW = "MY:WDR"
CB_REPEAT = "MY:RPT"

# Сколько последних заявок показываем в чате.
LIST_LIMIT = 8

# Значок статуса в списке.
_STATUS_ICONS = {
    "Новая": "⏳",
    "Оплачена": "✅",
    "Отложена": "⏸",
    "Отклонена": "❌",
    "Отозвана": "🚫",
}


def _icon(status: str) -> str:
    return _STATUS_ICONS.get(status, "•")


def format_amount(raw: str) -> str:
    """«125000.50» → «125 000.50»; неразобранное значение отдаём как есть.

    Разбор — канонический parse_amount: Google возвращает суммы уже
    отформатированными, с неразрывным пробелом в разделителе тысяч.
    """
    try:
        return f"{parse_amount(raw):,.2f}".replace(",", " ")
    except ValidationError:
        return raw


def repeat_webapp_url(request_id: str) -> str:
    """URL Mini App, открытого на форме с заполненными полями прошлой заявки."""
    url = settings.webapp_url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}repeat={request_id}"


def _format_list(rows: list[dict[str, str]], reasons: dict[str, str]) -> str:
    """Текст списка заявок (HTML)."""
    e = html.escape
    parts = ["📋 <b>Ваши последние заявки</b>"]
    for i, row in enumerate(rows, start=1):
        status = row.get("Статус оплаты", "")
        request_id = row.get("ID заявки", "")
        line = (
            f"\n<b>{i}.</b> {_icon(status)} <b>{e(status or '—')}</b>\n"
            f"🏢 {e(row.get('Контрагент', '—'))}\n"
            f"💰 {e(format_amount(row.get('Сумма', '')))} {e(row.get('Валюта', ''))}\n"
            f"📅 Оплатить до: {e(row.get('Плановая дата оплаты', '—') or '—')}\n"
            f"📄 Срок работ: "
            f"{e(row.get('Срок исполнения работ по договору', '—') or '—')}\n"
            f"<code>{e(request_id)}</code>"
        )
        reason = reasons.get(request_id)
        if reason:
            line += f"\n💬 Причина: {e(reason)}"
        parts.append(line)
    return "\n".join(parts)


def _build_keyboard(rows: list[dict[str, str]]) -> InlineKeyboardMarkup:
    """Кнопки под списком: отзыв, и только у заявки со статусом «Новая».

    Больше в чате действий нет: правка, напоминание о просрочке и
    закрывающие документы живут в Mini App, где для них есть место в ряду.
    """
    keyboard: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(rows, start=1):
        request_id = row.get("ID заявки", "")
        if not request_id:
            continue
        # «Повторить» убрана 27.08.2026: ряд кнопок не вмещал закрывающие
        # документы, а повтор дублировал обычную подачу. Обработчик
        # (bot/handlers.repeat_start) и ссылка ОСТАВЛЕНЫ: кнопки уже разосланы
        # в старые сообщения, и нажатие там не должно упираться в тишину.
        buttons: list[InlineKeyboardButton] = []
        if row.get("Статус оплаты", "") == STATUS_NEW:
            buttons.append(
                InlineKeyboardButton(
                    f"🚫 Отозвать №{i}", callback_data=f"{CB_WITHDRAW}:{request_id}"
                )
            )
        # Пустой ряд Telegram рисует пустотой: у оплаченной заявки кнопок
        # теперь может не быть вовсе, и такой ряд добавлять нельзя.
        if buttons:
            keyboard.append(buttons)
    return InlineKeyboardMarkup(keyboard)


# Что отвечаем, когда реестр не прочитался: «заявок нет» здесь было бы
# неправдой — человек решит, что его заявка пропала.
REGISTRY_DOWN = (
    "⚠️ Реестр сейчас недоступен, список показать не могу. "
    "Попробуйте через минуту — заявки на месте."
)


async def _load(user_id: int) -> tuple[list[dict[str, str]], dict[str, str]]:
    rows = await storage.recent_by_author(user_id, limit=LIST_LIMIT, strict=True)
    reasons = await request_meta.reasons_for([r.get("ID заявки", "") for r in rows])
    return rows, reasons


async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /my — список своих заявок. В группе только зовёт в личку."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None:
        return

    if chat is not None and chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "📋 Список заявок покажу в личке — там суммы и контрагенты "
            "видны только вам.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Открыть личку с ботом",
                    url=f"https://t.me/{context.bot.username}?start=my",
                )
            ]]),
        )
        return

    if not is_allowed(user.id):
        await message.reply_text(access_denied_message())
        return

    try:
        rows, reasons = await _load(user.id)
    except storage.RegistryUnavailable:
        await message.reply_text(REGISTRY_DOWN)
        return
    if not rows:
        await message.reply_text(
            "Заявок пока нет. Подать первую — /invoice или кнопка в меню (/menu)."
        )
        return

    await message.reply_text(
        _format_list(rows, reasons),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_keyboard(rows),
        disable_web_page_preview=True,
    )


async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🚫 Отозвать» в списке «Мои заявки»."""
    query = update.callback_query
    user = update.effective_user
    request_id = (query.data or "").split(":", 2)[-1]
    if user is None:
        await query.answer()
        return

    actor = f"@{user.username}" if user.username else user.full_name
    ok, message = await withdraw_request(
        context.bot, request_id, actor_id=user.id, actor_name=actor
    )
    await query.answer(message, show_alert=not ok)
    if not ok:
        return

    # Перерисовываем список: статус и набор кнопок изменились. Отзыв уже
    # прошёл, поэтому недоступный реестр здесь не ошибка — просто оставляем
    # прежнее сообщение, оно устарело только в одной строке.
    try:
        rows, reasons = await _load(user.id)
    except storage.RegistryUnavailable:
        log.info("Список «Мои заявки» не перерисован: реестр недоступен")
        return
    try:
        await query.edit_message_text(
            _format_list(rows, reasons),
            parse_mode=ParseMode.HTML,
            reply_markup=_build_keyboard(rows),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 — сообщение могло устареть или не измениться
        log.debug("Список «Мои заявки» не обновлён", exc_info=True)
