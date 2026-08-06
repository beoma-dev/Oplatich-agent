"""Команда /help: что доступно ЭТОМУ человеку, с учётом его прав.

Список команд один на всех был бы вредной подсказкой: сотрудник видел бы
админские команды, которые ему всё равно откажут, а финансист не понял бы,
что панель заявок открывается кнопкой.
"""
from __future__ import annotations

import html

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.access import is_allowed, is_bot_admin, is_financier

# (команда, описание). Порядок — от частого к редкому.
COMMON = [
    ("/invoice", "подать заявку на оплату"),
    ("/my", "мои заявки: статусы, повтор, отзыв"),
    ("/menu", "кнопки бота (в группе — закрепить кнопку подачи)"),
    ("/help", "этот список"),
    ("/myid", "показать мой Telegram ID"),
]

FINANCE = [
    ("📊 в форме", "панель всех заявок: фильтры, итоги, смена статуса"),
    ("⏰ напоминания", "настройка своего времени рассылки — вкладка в ⚙️"),
]

ADMIN = [
    ("/admin", "сводка настроек бота"),
    ("/allow", "открыть доступ к подаче: /allow @user или id"),
    ("/deny", "закрыть доступ: /deny @user или id"),
    ("/fin_add", "добавить финансиста: /fin_add @user или id"),
    ("/fin_del", "убрать финансиста: /fin_del @user или id"),
    ("/export", "выгрузить реестр заявок в xlsx"),
    ("/backup", "собрать архив всех данных прямо сейчас"),
    ("/audit", "последние события журнала безопасности"),
]

NO_ACCESS_NOTE = (
    "\n\n⛔ Подача заявок вам сейчас закрыта. Нажмите /start и «Запросить "
    "доступ» — админы получат заявку и решат."
)


def _block(title: str, rows: list[tuple[str, str]]) -> str:
    lines = [f"<b>{title}</b>"]
    lines += [f"{html.escape(cmd)} — {html.escape(text)}" for cmd, text in rows]
    return "\n".join(lines)


async def build_help(bot, user_id: int) -> str:
    """Собирает подсказку под права конкретного человека."""
    parts = ["📖 <b>Что я умею</b>", "", _block("Всем", COMMON)]
    if is_financier(user_id):
        parts += ["", _block("Финансисту", FINANCE)]
    if await is_bot_admin(bot, user_id):
        parts += ["", _block("Админу", ADMIN)]
    text = "\n".join(parts)
    if not is_allowed(user_id):
        text += NO_ACCESS_NOTE
    return text


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — список команд, доступных именно этому пользователю."""
    user = update.effective_user
    if user is None:
        return
    await update.effective_message.reply_text(
        await build_help(context.bot, user.id), parse_mode=ParseMode.HTML
    )
