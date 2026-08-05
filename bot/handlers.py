"""Диалог подачи заявки: команда /invoice запускает пошаговую форму.

Работает и в личке, и в группе (несколько человек). Заявка принимается ТОЛЬКО
через этот структурированный диалог — свободное сообщение заявкой не считается.

Поток (7 шагов):
  сумма → валюта → контрагент → статья → срочность+дата
  (срочно → сегодня, обычная → след. рабочий день, настраиваемая → ввод) →
  комментарий → «счёт есть?» ─ да → файл счёта
                             └ нет → реквизиты для оплаты
  (+ подтверждение, если найден дубль заявки)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.access import access_denied_message, is_allowed, is_bot_admin
from bot.access_requests import ask_access_markup
from bot.models import ARTICLES, CURRENCIES, InvoiceRequest, Urgency, new_request_id
from bot.my_requests import CB_REPEAT, my_command
from bot.scheduling import auto_planned_date
from bot.validators import (
    ValidationError,
    parse_amount,
    parse_planned_date,
    validate_file,
    validate_text_field,
)
from config import settings
from services import audit, dedup, invoice_check, storage
from services import runtime_settings as rs
from services.intake import finalize_submission
from services.local_storage import build_invoice_filename

log = logging.getLogger(__name__)

# Состояния диалога.
(
    AMOUNT,
    CURRENCY,
    COUNTERPARTY,
    ARTICLE,
    ARTICLE_CUSTOM,
    PLANNED_DATE,
    COMMENT,
    URGENCY,
    INVOICE_CHOICE,
    FILE,
    REQUISITES,
    CONFIRM_SUBMIT,
    DUP_CONFIRM,
) = range(13)

# Ключи во временном user_data (живут только в рамках одного диалога).
K_AMOUNT = "amount"
K_CURRENCY = "currency"
K_COUNTERPARTY = "counterparty"
K_ARTICLE = "article"
K_PLANNED = "planned_date"
K_COMMENT = "comment"
K_URGENCY = "urgency"
K_HAS_INVOICE = "has_invoice"
K_RETURN_CHAT = "return_chat"   # id группы, куда вернуть итоговое сообщение
K_PENDING_REQ = "pending_request"   # заявка, ждущая подтверждения дубля
K_PENDING_FILE = "pending_file"     # байты файла счёта для неё
K_PENDING_WARN = "pending_warning"  # предупреждение автопроверки файла

# callback_data подтверждения дубля
CB_DUP_YES = "DUP_YES"
CB_DUP_NO = "DUP_NO"

# callback_data пропуска комментария (комментарий необязателен)
CB_COMMENT_SKIP = "CMT_SKIP"

# callback_data экрана подтверждения (по ТЗ: запись — после подтверждения)
CB_SUBMIT_YES = "SUB_YES"
CB_SUBMIT_NO = "SUB_NO"

# callback_data
CB_INVOICE_YES = "INV_YES"
CB_INVOICE_NO = "INV_NO"
CB_START = "START_INVOICE"
CB_HELP = "HELP"

# Короткий фолбэк для alert (лимит Telegram — 200 символов): показывается,
# только если полную инструкцию не удалось отправить в личку.
HELP_ALERT = (
    "Заявка: сумма → контрагент → статья → срочность (дата — сама) → "
    "счёт или реквизиты → подтверждение. Полная инструкция — напишите боту "
    "/start и нажмите «Инструкция»."
)

# Полная инструкция — фолбэк, когда Mini App выключен и «❓ Инструкция»
# не может открыть окно приложения: уходит сообщением в личку.
HELP_TEXT = (
    "📖 <b>Как подать заявку на оплату</b>\n\n"
    "1️⃣ Нажмите <b>«🧾 Подать заявку»</b> — форма откроется прямо в Telegram.\n\n"
    "2️⃣ Заполните поля:\n"
    "• <b>Сумма и валюта</b> — «125 000,50» тоже поймёт;\n"
    "• <b>Контрагент</b> — подсказки из прошлых заявок подставляются "
    "вместе с реквизитами;\n"
    "• <b>Статья расходов</b> — выбор из списка или своя;\n"
    "• <b>Срочность</b>: 🔴 Срочно — оплата сегодня, 🟢 Обычная — следующий "
    "рабочий день, 🗓 Настраиваемая — дата в календаре;\n"
    "• <b>Комментарий</b> — по желанию.\n\n"
    "3️⃣ Приложите <b>файл счёта</b> (PDF/JPG/PNG/XLSX до 20 МБ) — бот сразу "
    "проверит, что это похоже на счёт, и (бета) предложит заполнить сумму, "
    "контрагента и реквизиты по распознанному счёту. Если счёта нет — введите "
    "<b>реквизиты</b> (поле прячется глазком 👁, у финансиста они под "
    "спойлером). Реквизиты проверяются: ИНН, БИК и счета — по контрольным "
    "суммам.\n\n"
    "4️⃣ Нажмите <b>«Отправить заявку»</b>.\n\n"
    "<b>Что будет дальше</b>\n"
    "• Вам придёт подтверждение с номером заявки и PDF-документом.\n"
    "• В общий чат уйдёт краткий итог — <i>без файла и реквизитов</i>.\n"
    "• Финансист отметит статус: ✅ Оплачено / ⏸ Отложено / ❌ Отклонено — "
    "вы получите уведомление, при отказе или отсрочке — с причиной.\n\n"
    "<b>📋 Мои заявки</b>\n"
    "Команда /my (или кнопка 📋 в форме) показывает ваши последние заявки со "
    "статусами и причинами отказа. Оттуда же:\n"
    "• <b>✏️ Изменить</b> (в форме) — исправить заявку, пока финансист её не "
    "тронул: прежняя отзывается, поля переносятся в форму;\n"
    "• <b>↻ Повторить</b> — новая заявка с теми же контрагентом, суммой, "
    "статьёй и реквизитами (аренда, хостинг и прочее ежемесячное);\n"
    "• <b>🚫 Отозвать</b> — забрать заявку совсем; финансисты получат "
    "уведомление.\n\n"
    "<b>Полезное</b>\n"
    "• Черновик сохраняется сам — случайно закрытая форма ничего не теряет; "
    "сбросить его можно кнопкой «Очистить форму» в конце формы.\n"
    "• Кнопка ⚙️ в форме открывает настройки: там же вкладка оформления — "
    "тема (телеграмная или неоновая) и живой фон; выбор запоминается.\n"
    "• /invoice — новая заявка, /my — мои заявки, /myid — ваш ID для доступа.\n"
    "• Доступ по белому списку: если бот не пускает — нажмите «Запросить "
    "доступ», админы получат заявку и решат."
)


DEEPLINK_PREFIX = "inv_"  # payload для deep-link: inv_<group_chat_id>


def webapp_form_url(return_chat_id: int | None = None) -> str:
    """URL формы Mini App (с параметром возврата итога в группу)."""
    url = settings.webapp_url
    if return_chat_id is not None:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}return_chat={return_chat_id}"
    return url


def webapp_help_url() -> str:
    """URL Mini App, открытого сразу на экране инструкции."""
    url = settings.webapp_url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}help=1"


def build_menu_markup() -> InlineKeyboardMarkup:
    """Кнопки запуска формы в ЛИЧКЕ.

    С включённым Mini App кнопка открывает форму-страницу; иначе callback —
    пошаговая форма стартует прямо в чате.
    """
    if settings.webapp_enabled:
        submit_button = InlineKeyboardButton(
            "🧾 Подать заявку на оплату", web_app=WebAppInfo(url=webapp_form_url())
        )
        # Инструкция открывается окном Mini App, а не сообщением от бота.
        help_button = InlineKeyboardButton(
            "❓ Инструкция", web_app=WebAppInfo(url=webapp_help_url())
        )
    else:
        submit_button = InlineKeyboardButton(
            "🧾 Подать заявку на оплату", callback_data=CB_START
        )
        help_button = InlineKeyboardButton("❓ Инструкция", callback_data=CB_HELP)
    return InlineKeyboardMarkup([[submit_button], [help_button]])


def build_group_button(bot_username: str, group_chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки для ГРУППЫ/КАНАЛА + всплывающая инструкция.

    С зарегистрированным Mini App (MINIAPP_SHORT_NAME) кнопка открывает форму
    прямой ссылкой t.me/<бот>/<имя>?startapp=<chat_id> сразу поверх чата.
    Иначе — deep-link в личку с ботом (web_app-кнопки в группах/каналах
    Telegram не разрешает). Итог заявки в любом случае вернётся в этот чат.
    """
    if settings.webapp_enabled and settings.miniapp_short_name:
        url = (
            f"https://t.me/{bot_username}/{settings.miniapp_short_name}"
            f"?startapp={group_chat_id}"
        )
        # Та же прямая ссылка, но приложение откроется на экране инструкции.
        help_button = InlineKeyboardButton(
            "❓ Инструкция",
            url=f"https://t.me/{bot_username}/{settings.miniapp_short_name}?startapp=help",
        )
    else:
        url = f"https://t.me/{bot_username}?start={DEEPLINK_PREFIX}{group_chat_id}"
        help_button = InlineKeyboardButton("❓ Инструкция", callback_data=CB_HELP)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧾 Подать заявку на оплату", url=url)],
            [help_button],
        ]
    )


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _addr(update: Update) -> str:
    """Обращение к автору в группе (по имени), чтобы было понятно, кому вопрос."""
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and user:
        return f"{user.first_name}, "
    return ""


def _build_request(context: ContextTypes.DEFAULT_TYPE, update: Update, now: datetime) -> InvoiceRequest:
    """Собирает заявку из накопленного user_data (без файла/реквизитов)."""
    user = update.effective_user
    return InvoiceRequest(
        telegram_id=user.id,
        sender_username=f"@{user.username}" if user.username else "—",
        # Подтверждённое ФИО из справочника СБ, а не переименовываемый профиль.
        sender_name=settings.employee_name_for(user.id) or user.full_name,
        amount=context.user_data[K_AMOUNT],
        currency=context.user_data[K_CURRENCY],
        counterparty=context.user_data[K_COUNTERPARTY],
        article=context.user_data[K_ARTICLE],
        planned_date=context.user_data[K_PLANNED],
        comment=context.user_data[K_COMMENT],
        urgency=context.user_data[K_URGENCY],
        has_invoice=context.user_data[K_HAS_INVOICE],
        created_at=now,
        request_id=new_request_id(now, user.id),
    )


async def _persist_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: InvoiceRequest,
    invoice_file: bytes | None = None,
    file_warning: str | None = None,
) -> int:
    """Финализирует заявку (реестр, уведомления, подтверждение, итог в группу).

    Вся логика — в services.intake, общая с API мини-приложения.
    """
    message = update.effective_message
    try:
        await finalize_submission(
            context.bot,
            request,
            return_chat_id=context.user_data.get(K_RETURN_CHAT),
            invoice_file=invoice_file,
            file_warning=file_warning,
        )
    except Exception:  # noqa: BLE001
        log.exception("Ошибка сохранения заявки %s", request.request_id)
        await message.reply_text(
            "❌ Не удалось сохранить заявку из-за технической ошибки. "
            "Попробуйте ещё раз позже или сообщите администратору."
        )
    finally:
        context.user_data.clear()
    return ConversationHandler.END


async def _do_finalize(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: InvoiceRequest,
    invoice_file: bytes | None,
    file_warning: str | None = None,
) -> int:
    """Сохраняет файл счёта (если ещё не сохранён) и финализирует заявку."""
    if invoice_file is not None and not request.file_url:
        try:
            request.file_url = await storage.save_invoice(invoice_file, request.file_name)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка сохранения файла заявки %s", request.request_id)
            await update.effective_message.reply_text(
                "❌ Не удалось сохранить файл счёта. Попробуйте ещё раз позже."
            )
            context.user_data.clear()
            return ConversationHandler.END
    return await _persist_and_reply(
        update, context, request, invoice_file=invoice_file, file_warning=file_warning
    )


async def _ask_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: InvoiceRequest,
    invoice_file: bytes | None,
    file_warning: str | None,
) -> int:
    """Экран «Проверьте заявку»: по ТЗ запись — только после подтверждения."""
    import html as _html

    e = _html.escape
    context.user_data[K_PENDING_REQ] = request
    context.user_data[K_PENDING_FILE] = invoice_file
    context.user_data[K_PENDING_WARN] = file_warning

    planned = request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    source = "📎 файл счёта приложен" if request.has_invoice else "✍️ по реквизитам (без счёта)"
    text = (
        "🧾 <b>Проверьте заявку</b>\n\n"
        f"💰 Сумма: <b>{e(f'{request.amount:,.2f}')} {e(request.currency)}</b>\n"
        f"🏢 Контрагент: {e(request.counterparty)}\n"
        f"📂 Статья: {e(request.article)}\n"
        f"📅 Оплатить до: <b>{e(planned)}</b>\n"
        f"⏱ Срочность: {request.urgency.value}\n"
        f"💬 Комментарий: {e(request.comment or '—')}\n"
        f"{source}"
    )
    if file_warning:
        text += f"\n{e(file_warning)}"
    text += "\n\nОтправить заявку?"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить", callback_data=CB_SUBMIT_YES)],
        [InlineKeyboardButton("❌ Отменить", callback_data=CB_SUBMIT_NO)],
    ])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    return CONFIRM_SUBMIT


async def submit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ на экран подтверждения заявки."""
    query = update.callback_query
    await query.answer()
    request = context.user_data.get(K_PENDING_REQ)

    if query.data != CB_SUBMIT_YES or request is None:
        await query.edit_message_text("Заявка отменена. Начать заново — /invoice")
        context.user_data.clear()
        return ConversationHandler.END

    invoice_file = context.user_data.get(K_PENDING_FILE)
    file_warning = context.user_data.get(K_PENDING_WARN)
    await query.edit_message_text("⏳ Отправляю заявку…")
    # Дальше — проверка на дубль (может задать свой отдельный вопрос).
    return await _confirm_or_finalize(update, context, request, invoice_file, file_warning)


async def _confirm_or_finalize(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: InvoiceRequest,
    invoice_file: bytes | None,
    file_warning: str | None = None,
) -> int:
    """Дедуп: если похожая заявка уже подавалась — просим подтверждение."""
    last_seen = await dedup.check_duplicate(request)
    if last_seen is None:
        return await _do_finalize(update, context, request, invoice_file, file_warning)

    context.user_data[K_PENDING_REQ] = request
    context.user_data[K_PENDING_FILE] = invoice_file
    context.user_data[K_PENDING_WARN] = file_warning
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Всё равно отправить", callback_data=CB_DUP_YES)],
        [InlineKeyboardButton("❌ Отменить", callback_data=CB_DUP_NO)],
    ])
    await update.effective_message.reply_text(
        f"⚠️ Похоже, такая заявка уже подавалась <b>{last_seen}</b>: совпадают "
        "контрагент, сумма, валюта, статья и плановая дата.\n\n"
        "Отправить ещё раз?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return DUP_CONFIRM


async def dup_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ на предупреждение о дубле."""
    query = update.callback_query
    await query.answer()
    request = context.user_data.get(K_PENDING_REQ)

    if query.data != CB_DUP_YES or request is None:
        await query.edit_message_text("Заявка отменена. Начать заново — /invoice")
        context.user_data.clear()
        return ConversationHandler.END

    invoice_file = context.user_data.get(K_PENDING_FILE)
    file_warning = context.user_data.get(K_PENDING_WARN)
    await query.edit_message_text("Дубль подтверждён — отправляю заявку ✅")
    await audit.log_event(
        audit.DUPLICATE_CONFIRMED,
        request.telegram_id,
        request.sender_username,
        request.request_id,
    )
    return await _do_finalize(update, context, request, invoice_file, file_warning)


# ---------------------------------------------------------------------------
# Шаги диалога
# ---------------------------------------------------------------------------
def _is_group(update: Update) -> bool:
    """Групповой чат ИЛИ канал: форма запускается кнопкой-ссылкой в личку."""
    chat = update.effective_chat
    return bool(
        chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL)
    )


async def _post_group_button(update: Update, context: ContextTypes.DEFAULT_TYPE, *, pin: bool) -> None:
    """Публикует в группе/канале кнопку-ссылку в личку и (опц.) закрепляет её."""
    chat = update.effective_chat
    markup = build_group_button(context.bot.username, chat.id)
    msg = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "🧾 <b>Заявки на оплату</b>\n\n"
            "Нажмите <b>«🧾 Подать заявку на оплату»</b> — откроется форма, "
            "где вы приватно заполните:\n"
            "• сумму и валюту;\n"
            "• контрагента (кому платим);\n"
            "• статью расходов;\n"
            "• срочность и плановую дату оплаты — 🔴 срочно = сегодня, "
            "🟢 обычная = следующий рабочий день, 🗓 или своя дата;\n"
            "• комментарий (по желанию);\n"
            "• файл счёта <i>или</i> реквизиты (если счёта нет).\n\n"
            "🔒 Данные <b>не видны</b> в этом чате — сюда придёт только "
            "краткое уведомление о созданной заявке.\n"
            "❓ Кнопка <b>«Инструкция»</b> откроет подробную памятку.\n\n"
            "⚠️ <b>В первый раз</b> откройте бота — "
            f'<a href="https://t.me/{context.bot.username}">@{context.bot.username}</a> '
            "— и нажмите <b>«Старт»</b> (команда /start). Без этого бот не "
            "сможет прислать вам подтверждение и файл заявки в личку."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        # Ссылка на бота — служебная, превью карточки бота в посте лишнее.
        disable_web_page_preview=True,
    )
    log.info("Кнопка подачи заявки опубликована в чате %s (%s)", chat.id, chat.type)
    if pin:
        try:
            await context.bot.pin_chat_message(
                chat_id=chat.id, message_id=msg.message_id, disable_notification=True
            )
        except Exception as exc:  # noqa: BLE001 — нет прав на закрепление и т.п.
            log.warning("Не удалось закрепить кнопку в чате %s: %s", chat.id, exc)
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "ℹ️ Не смог закрепить кнопку — назначьте меня "
                    "<b>администратором</b> с правом «Закреплять сообщения», "
                    "и снова вызовите /menu."
                ),
                parse_mode=ParseMode.HTML,
            )


async def _begin_form(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_to,
    *,
    return_chat_id: int | None = None,
) -> int:
    """Общий старт формы (в личном чате)."""
    user = update.effective_user
    if user is None or not is_allowed(user.id):
        if user is not None:
            await audit.log_event(
                audit.ACCESS_DENIED,
                user.id,
                f"@{user.username}" if user.username else user.full_name,
                "чат-форма",
            )
        await reply_to.reply_text(
            access_denied_message(), reply_markup=ask_access_markup()
        )
        return ConversationHandler.END

    # С включённым Mini App вместо пошагового диалога отдаём кнопку-форму.
    # return_chat уезжает параметром URL и вернётся с данными формы.
    if settings.webapp_enabled:
        await reply_to.reply_text(
            "🧾 Нажмите кнопку — откроется форма заявки:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "🧾 Открыть форму",
                    web_app=WebAppInfo(url=webapp_form_url(return_chat_id)),
                )]]
            ),
        )
        return ConversationHandler.END

    context.user_data.clear()
    if return_chat_id is not None:
        context.user_data[K_RETURN_CHAT] = return_chat_id
    await reply_to.reply_text(
        "🧾 <b>Новая заявка на оплату</b>\n\n"
        "Шаг 1 из 7 — введите <b>сумму</b> платежа "
        "(например: 125000 или 125000.50).\n\n"
        "Отменить в любой момент — /cancel",
        parse_mode=ParseMode.HTML,
    )
    return AMOUNT


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /start.

    - /start inv_<group_id> (deep-link из группы) → старт формы в личке с
      возвратом итога в группу;
    - обычный /start в личке → приветствие с кнопкой;
    - /start в группе → публикует кнопку-ссылку в личку.
    """
    args = context.args or []
    # Кнопка «Открыть личку с ботом» из группового ответа на /my.
    if args and args[0] == "my":
        await my_command(update, context)
        return ConversationHandler.END
    if args and args[0].startswith(DEEPLINK_PREFIX):
        raw = args[0][len(DEEPLINK_PREFIX):]
        try:
            return_chat_id = int(raw)
        except ValueError:
            return_chat_id = None
        return await _begin_form(update, context, update.message, return_chat_id=return_chat_id)

    if _is_group(update):
        await _post_group_button(update, context, pin=False)
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я Оплатыч — принимаю счета на оплату.\n\n"
        "Нажмите кнопку ниже, чтобы заполнить заявку приватно.",
        reply_markup=build_menu_markup(),
    )
    return ConversationHandler.END


async def start_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /invoice. В группе — отправляет кнопку в личку, в личке — старт формы."""
    if _is_group(update):
        await _post_group_button(update, context, pin=False)
        return ConversationHandler.END
    return await _begin_form(update, context, update.message)


async def start_invoice_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Подать заявку» в личке (callback) — старт формы на месте."""
    query = update.callback_query
    await query.answer()
    return await _begin_form(update, context, query.message)


async def help_popup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Инструкция»: полная инструкция уходит в личку нажавшему.

    Alert Telegram ограничен 200 символами — для нормальной инструкции
    этого мало. Если личка закрыта (человек не писал боту) — показываем
    короткий фолбэк с подсказкой открыть чат с ботом.
    """
    query = update.callback_query
    user = update.effective_user
    try:
        await context.bot.send_message(
            chat_id=user.id, text=HELP_TEXT, parse_mode=ParseMode.HTML
        )
    except Exception:  # noqa: BLE001 — пользователь ещё не открывал личку
        await query.answer(text=HELP_ALERT, show_alert=True)
        return
    chat = update.effective_chat
    if chat is not None and chat.type == ChatType.PRIVATE:
        await query.answer()
    else:
        await query.answer("📖 Инструкция отправлена вам в личку.")


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /menu. В группе/канале публикует и закрепляет кнопку-ссылку в личку."""
    if _is_group(update):
        await _post_group_button(update, context, pin=True)
    else:
        await update.effective_message.reply_text(
            "Нажмите кнопку, чтобы подать заявку 👇",
            reply_markup=build_menu_markup(),
        )


async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команды /menu, /invoice, /start в постах КАНАЛА → публикация кнопки.

    CommandHandler в PTB не обрабатывает channel_post, поэтому для каналов —
    отдельный обработчик (регэксп в main.py).
    """
    await _post_group_button(update, context, pin=True)


# Статусы «бот состоит в чате» для my_chat_member.
_JOINED_STATUSES = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
_LEFT_STATUSES = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


async def bot_membership_changed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бота добавили в группу или канал — публикует и закрепляет кнопку-ссылку.

    Работает через my_chat_member: в каналах сообщения NEW_CHAT_MEMBERS не
    приходят вовсе, а этот апдейт Telegram шлёт и для групп, и для каналов.
    Кнопка публикуется только при переходе «не состоял → состоит», чтобы не
    спамить при каждом изменении прав.
    """
    change = update.my_chat_member
    chat = update.effective_chat
    if change is None or chat is None:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return

    joined = (
        change.new_chat_member.status in _JOINED_STATUSES
        and change.old_chat_member.status in _LEFT_STATUSES
    )
    left = (
        change.new_chat_member.status in _LEFT_STATUSES
        and change.old_chat_member.status in _JOINED_STATUSES
    )

    if left:
        # Бота удалили — чат больше не источник прав админа.
        await asyncio.to_thread(rs.forget_admin_chat, chat.id)
        return

    if not joined:
        return

    # Доверенный чат (его админы = админы бота) — только если бота добавил
    # существующий админ. Иначе любой мог бы получить права, добавив бота
    # в свою группу.
    adder = update.effective_user
    if adder is not None and await is_bot_admin(context.bot, adder.id):
        await asyncio.to_thread(
            rs.remember_admin_chat, chat.id, chat.title or str(chat.id)
        )
    else:
        log.info(
            "Чат %s добавлен не админом бота — права админов чата не выдаются", chat.id
        )

    try:
        await _post_group_button(update, context, pin=True)
    except Exception:  # noqa: BLE001 — например, в канале нет права публикации
        log.exception(
            "Не удалось опубликовать кнопку в чате %s после добавления", chat.id
        )


async def step_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = parse_amount(update.message.text)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}\nПовторите ввод суммы.")
        return AMOUNT

    context.user_data[K_AMOUNT] = amount
    # Клавиатура валют (по 3 в ряд).
    rows = [CURRENCIES[i : i + 3] for i in range(0, len(CURRENCIES), 3)]
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(c, callback_data=f"CUR:{c}") for c in row] for row in rows]
    )
    await update.message.reply_text(
        f"{_addr(update)}шаг 2 из 7 — выберите <b>валюту</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return CURRENCY


async def step_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[-1]
    if code not in CURRENCIES:
        await query.edit_message_text("⚠️ Некорректная валюта. Начните заново: /invoice")
        return ConversationHandler.END

    context.user_data[K_CURRENCY] = code
    await query.edit_message_text(f"Валюта: {code}")
    await query.message.reply_text(
        f"{_addr(update)}шаг 3 из 7 — введите <b>контрагента</b> (кому платим).",
        parse_mode=ParseMode.HTML,
    )
    return COUNTERPARTY


def _article_keyboard() -> InlineKeyboardMarkup:
    """Статьи расходов по две в ряд + кнопка «Своя статья»."""
    rows = []
    for j in range(0, len(ARTICLES), 2):
        rows.append([
            InlineKeyboardButton(a, callback_data=f"ART:{j + k}")
            for k, a in enumerate(ARTICLES[j:j + 2])
        ])
    rows.append([InlineKeyboardButton("✏️ Своя статья", callback_data="ART:CUSTOM")])
    return InlineKeyboardMarkup(rows)


async def step_counterparty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = validate_text_field(update.message.text, field_name="Контрагент", max_len=200)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return COUNTERPARTY

    context.user_data[K_COUNTERPARTY] = value
    await update.message.reply_text(
        f"{_addr(update)}шаг 4 из 7 — выберите <b>статью расходов</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=_article_keyboard(),
    )
    return ARTICLE


async def step_article(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[-1]

    if code == "CUSTOM":
        await query.edit_message_text("Статья: своя ✏️")
        await query.message.reply_text(
            f"{_addr(update)}введите <b>название статьи расходов</b>.",
            parse_mode=ParseMode.HTML,
        )
        return ARTICLE_CUSTOM

    try:
        article = ARTICLES[int(code)]
    except (ValueError, IndexError):
        await query.edit_message_text("⚠️ Некорректная статья. Начните заново: /invoice")
        return ConversationHandler.END

    context.user_data[K_ARTICLE] = article
    await query.edit_message_text(f"Статья: {article}")
    await query.message.reply_text(
        f"{_addr(update)}шаг 5 из 7 — выберите <b>срочность</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=_urgency_keyboard(),
    )
    return URGENCY


async def step_article_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = validate_text_field(update.message.text, field_name="Статья", max_len=100)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return ARTICLE_CUSTOM

    context.user_data[K_ARTICLE] = value
    await update.message.reply_text(
        f"{_addr(update)}шаг 5 из 7 — выберите <b>срочность</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=_urgency_keyboard(),
    )
    return URGENCY


def _urgency_keyboard() -> InlineKeyboardMarkup:
    """Срочность определяет дату: сегодня / следующий рабочий / вручную."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Срочно", callback_data="URG:URGENT"),
            InlineKeyboardButton("🟢 Обычная", callback_data="URG:NORMAL"),
        ],
        [InlineKeyboardButton("🗓 Настраиваемая дата", callback_data="URG:CUSTOM")],
    ])


_ASK_COMMENT = "шаг 6 из 7 — введите <b>комментарий</b> (необязательно)."


def _comment_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ Пропустить", callback_data=CB_COMMENT_SKIP)]]
    )


def _invoice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📎 Счёт есть", callback_data=CB_INVOICE_YES),
                InlineKeyboardButton("✍️ Счёта нет", callback_data=CB_INVOICE_NO),
            ]
        ]
    )


_ASK_INVOICE = "шаг 7 из 7 — есть ли <b>файл счёта</b>?"


async def step_urgency_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Срочность + дата: срочно → сегодня, обычная → следующий рабочий день,
    настраиваемая → ручной ввод. Дата считается на сервере в TIMEZONE."""
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[-1]

    if code == "CUSTOM":
        context.user_data[K_URGENCY] = Urgency.NORMAL
        await query.edit_message_text("Срочность: обычная, дата — вручную 🗓")
        await query.message.reply_text(
            f"{_addr(update)}введите <b>плановую дату оплаты</b> (например: 15.08.2026).",
            parse_mode=ParseMode.HTML,
        )
        return PLANNED_DATE

    if code not in ("URGENT", "NORMAL"):
        await query.edit_message_text("⚠️ Некорректный выбор. Начните заново: /invoice")
        return ConversationHandler.END

    urgency = Urgency.URGENT if code == "URGENT" else Urgency.NORMAL
    planned = auto_planned_date(urgency.is_urgent)
    context.user_data[K_URGENCY] = urgency
    context.user_data[K_PLANNED] = planned
    when = "сегодня" if urgency.is_urgent else "следующий рабочий день"
    await query.edit_message_text(
        f"Срочность: {urgency.value} · оплата {when}, {planned.strftime('%d.%m.%Y')}"
    )
    await query.message.reply_text(
        f"{_addr(update)}{_ASK_COMMENT}",
        parse_mode=ParseMode.HTML,
        reply_markup=_comment_skip_keyboard(),
    )
    return COMMENT


async def step_planned_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        planned = parse_planned_date(update.message.text)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}\nПовторите ввод даты.")
        return PLANNED_DATE

    context.user_data[K_PLANNED] = planned
    await update.message.reply_text(
        f"{_addr(update)}{_ASK_COMMENT}",
        parse_mode=ParseMode.HTML,
        reply_markup=_comment_skip_keyboard(),
    )
    return COMMENT


async def step_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = validate_text_field(update.message.text, field_name="Комментарий", max_len=500)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return COMMENT

    context.user_data[K_COMMENT] = value
    await update.message.reply_text(
        f"{_addr(update)}{_ASK_INVOICE}",
        parse_mode=ParseMode.HTML,
        reply_markup=_invoice_keyboard(),
    )
    return INVOICE_CHOICE


async def comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Пропустить» — комментарий необязателен."""
    query = update.callback_query
    await query.answer()
    context.user_data[K_COMMENT] = ""
    await query.edit_message_text("Комментарий: —")
    await query.message.reply_text(
        f"{_addr(update)}{_ASK_INVOICE}",
        parse_mode=ParseMode.HTML,
        reply_markup=_invoice_keyboard(),
    )
    return INVOICE_CHOICE


async def step_invoice_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CB_INVOICE_YES:
        context.user_data[K_HAS_INVOICE] = True
        await query.edit_message_text("Счёт есть 📎")
        await query.message.reply_text(
            f"{_addr(update)}прикрепите <b>файл счёта</b> (PDF, JPG, PNG или XLSX).",
            parse_mode=ParseMode.HTML,
        )
        return FILE

    if query.data == CB_INVOICE_NO:
        context.user_data[K_HAS_INVOICE] = False
        await query.edit_message_text("Счёта нет ✍️")
        await query.message.reply_text(
            f"{_addr(update)}счёта нет — пришлите <b>реквизиты для оплаты</b> одним сообщением:\n"
            "• получатель\n• ИНН / КПП\n• расчётный счёт\n• банк и БИК\n• корр. счёт\n"
            "• назначение платежа",
            parse_mode=ParseMode.HTML,
        )
        return REQUISITES

    await query.edit_message_text("⚠️ Некорректный выбор. Начните заново: /invoice")
    return ConversationHandler.END


async def step_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    doc = message.document
    photo = message.photo[-1] if message.photo else None

    if doc is None and photo is None:
        await message.reply_text("⚠️ Пришлите файл счёта (документ или фото).")
        return FILE

    if doc is not None:
        mime_type = doc.mime_type
        file_size = doc.file_size
        original_name = doc.file_name or "invoice"
        tg_file_id = doc.file_id
    else:  # фото
        mime_type = "image/jpeg"
        file_size = photo.file_size
        original_name = "invoice.jpg"
        tg_file_id = photo.file_id

    try:
        validate_file(mime_type, file_size)
    except ValidationError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return FILE

    await message.reply_text("⏳ Сохраняю заявку…")

    now = _now()
    request = _build_request(context, update, now)
    try:
        tg_file = await context.bot.get_file(tg_file_id)
        content = bytes(await tg_file.download_as_bytearray())

        request.file_name = build_invoice_filename(
            original_name, request.counterparty, request.amount, now
        )
    except Exception:  # noqa: BLE001
        log.exception("Ошибка получения файла заявки %s", request.request_id)
        await message.reply_text(
            "❌ Не удалось получить файл счёта. Попробуйте ещё раз позже."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Мягкая автопроверка «похоже ли на счёт» (текст PDF / OCR).
    file_warning = await asyncio.to_thread(
        invoice_check.check_invoice_file, content, original_name, request.amount
    )
    # Файл сохраняется после подтверждения и проверки на дубль.
    return await _ask_confirmation(update, context, request, content, file_warning)


async def step_requisites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = validate_text_field(update.message.text, field_name="Реквизиты", max_len=1500)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return REQUISITES

    now = _now()
    request = _build_request(context, update, now)
    request.requisites = value
    return await _ask_confirmation(update, context, request, None, None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Заявка отменена. Начать заново — /invoice")
    return ConversationHandler.END


async def repeat_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «↻ Повторить» из /my — в режиме БЕЗ Mini App.

    Поля прошлой заявки (контрагент, сумма, валюта, статья, комментарий)
    подставляются, плановая дата считается заново по срочности — старая уже
    в прошлом. Остаётся приложить свежий счёт или ввести реквизиты.
    С включённым Mini App повтор открывает форму-страницу (?repeat=<id>),
    и этот вход не используется.
    """
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    request_id = (query.data or "").split(":", 2)[-1]

    if user is None or not is_allowed(user.id):
        await query.message.reply_text(
            access_denied_message(), reply_markup=ask_access_markup()
        )
        return ConversationHandler.END

    row = await storage.get_request(request_id)
    # Чужую заявку повторить нельзя: в ней контрагент и реквизиты чужого платежа.
    if row is None or row.get("Telegram ID", "") != str(user.id):
        await query.message.reply_text("⚠️ Заявка не найдена.")
        return ConversationHandler.END

    try:
        amount = parse_amount(row.get("Сумма", ""))
    except ValidationError:
        await query.message.reply_text(
            "⚠️ Не удалось разобрать сумму прошлой заявки — заполните форму заново: /invoice"
        )
        return ConversationHandler.END

    urgency = (
        Urgency.URGENT if row.get("Срочность", "") == Urgency.URGENT.value
        else Urgency.NORMAL
    )
    currency = row.get("Валюта", "") or CURRENCIES[0]

    context.user_data.clear()
    context.user_data[K_AMOUNT] = amount
    context.user_data[K_CURRENCY] = currency if currency in CURRENCIES else CURRENCIES[0]
    context.user_data[K_COUNTERPARTY] = row.get("Контрагент", "")
    context.user_data[K_ARTICLE] = row.get("Статья", "")
    context.user_data[K_COMMENT] = row.get("Комментарий", "")
    context.user_data[K_URGENCY] = urgency
    context.user_data[K_PLANNED] = auto_planned_date(urgency.is_urgent)

    import html as _html

    e = _html.escape
    planned = context.user_data[K_PLANNED].strftime("%d.%m.%Y")
    await query.message.reply_text(
        "↻ <b>Повтор заявки</b>\n\n"
        f"💰 Сумма: <b>{e(f'{amount:,.2f}')} {e(context.user_data[K_CURRENCY])}</b>\n"
        f"🏢 Контрагент: {e(context.user_data[K_COUNTERPARTY])}\n"
        f"📂 Статья: {e(context.user_data[K_ARTICLE] or '—')}\n"
        f"📅 Оплатить до: <b>{e(planned)}</b>\n\n"
        "Изменить что-то — /cancel и заполните форму заново.",
        parse_mode=ParseMode.HTML,
    )
    await query.message.reply_text(
        _ASK_INVOICE, parse_mode=ParseMode.HTML, reply_markup=_invoice_keyboard()
    )
    return INVOICE_CHOICE


def build_conversation_handler() -> ConversationHandler:
    """Собирает ConversationHandler формы /invoice (работает и в группе)."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("invoice", start_invoice),
            CallbackQueryHandler(start_invoice_button, pattern=rf"^{CB_START}$"),
            CallbackQueryHandler(repeat_start, pattern=rf"^{CB_REPEAT}:"),
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_amount)],
            CURRENCY: [CallbackQueryHandler(step_currency, pattern=r"^CUR:")],
            COUNTERPARTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_counterparty)],
            ARTICLE: [CallbackQueryHandler(step_article, pattern=r"^ART:")],
            ARTICLE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_article_custom)],
            PLANNED_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_planned_date)],
            COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_comment),
                CallbackQueryHandler(comment_skip, pattern=rf"^{CB_COMMENT_SKIP}$"),
            ],
            URGENCY: [CallbackQueryHandler(step_urgency_choice, pattern=r"^URG:")],
            INVOICE_CHOICE: [
                CallbackQueryHandler(step_invoice_choice, pattern=r"^INV_(YES|NO)$")
            ],
            FILE: [
                MessageHandler(
                    (filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, step_file
                )
            ],
            REQUISITES: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_requisites)],
            CONFIRM_SUBMIT: [
                CallbackQueryHandler(submit_confirm, pattern=r"^SUB_(YES|NO)$")
            ],
            DUP_CONFIRM: [CallbackQueryHandler(dup_confirm, pattern=r"^DUP_(YES|NO)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="invoice_form",
        persistent=True,  # рестарт бота не обрывает начатую форму
        conversation_timeout=600,  # 10 минут неактивности — сброс
        # per_chat=True, per_user=True (по умолчанию): в группе у каждого свой диалог.
    )
