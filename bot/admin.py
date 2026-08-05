"""Админ-команды в личке: управление финансистами и whitelist из Telegram.

Красивый способ — админ-панель в Mini App (шестерёнка в шапке формы);
эти команды — быстрый запасной путь и работают без HTTPS.

Админы задаются ADMIN_IDS в .env. Динамические списки хранятся в
data/bot_settings.json и объединяются со списками из .env.
"""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from bot.access import is_bot_admin
from config import settings
from services import audit
from services import runtime_settings as rs
from services.user_directory import resolve

log = logging.getLogger(__name__)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Доступна всем: подсказывает свой Telegram ID (для передачи админу)."""
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: <code>{user.id}</code>", parse_mode=ParseMode.HTML
    )


async def _admin_gate(update: Update) -> bool:
    """Пускает только админов и только в личке."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None:
        return False
    if chat and chat.type != ChatType.PRIVATE:
        return False
    if await is_bot_admin(update.get_bot(), user.id):
        return True
    if not rs.effective_admin_ids() and not rs.admin_chat_ids():
        await message.reply_text(
            "Админы не настроены. Задайте ADMIN_IDS в .env (ваш id — /myid) "
            "или добавьте бота в канал/группу от имени админа — тогда права "
            "получат все администраторы этого чата."
        )
    else:
        await audit.log_event(
            audit.ADMIN_DENIED,
            user.id,
            f"@{user.username}" if user.username else user.full_name,
            (message.text or "команда")[:50],
        )
        await message.reply_text(
            "⛔ Команда доступна только администраторам бота "
            "(админам канала/группы, где установлен бот, или из ADMIN_IDS)."
        )
    return False


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Панель: текущие настройки и подсказка по командам."""
    if not await _admin_gate(update):
        return
    e = html.escape

    fin_env = settings.finance_recipients
    fin_dyn = rs.dynamic_finance()
    fin_lines = [f"  · {e(x)} <i>(.env)</i>" for x in fin_env]
    fin_lines += [f"  · {e(x)}" for x in fin_dyn]

    allowed_env = settings.allowed_user_ids
    allowed_dyn = rs.dynamic_allowed()
    wl_total = len(allowed_env) + len(allowed_dyn)
    wl_note = "закрыт для всех (fail-closed) ⚠️" if wl_total == 0 else f"{wl_total} чел."

    cfg = rs.backup_config()
    backup_line = (
        f"\n\n💾 Бэкап: {'вкл' if cfg['enabled'] else 'выкл'} · "
        f"{cfg['time']} · хранить {cfg['keep']}"
    )

    text = (
        "⚙️ <b>Настройки invoice-bot</b>\n\n"
        f"💼 Финансисты ({len(fin_env) + len(fin_dyn)}):\n"
        + ("\n".join(fin_lines) if fin_lines else "  — не заданы ⚠️")
        + f"\n\n👥 Доступ к подаче: {wl_note}\n"
        + (
            "\n".join(f"  · <code>{i}</code> <i>(.env)</i>" for i in allowed_env)
            + ("\n" if allowed_env and allowed_dyn else "")
            + "\n".join(f"  · <code>{i}</code>" for i in allowed_dyn)
            if wl_total
            else ""
        )
        + backup_line
        + "\n\n<b>Команды</b>\n"
        "/fin_add @user | id — добавить финансиста\n"
        "/fin_del @user | id — убрать финансиста\n"
        "/allow @user | id — разрешить подачу заявок\n"
        "/deny @user | id — запретить подачу заявок\n"
        "/audit — последние события журнала безопасности\n"
        "/export — выгрузить актуальный xlsx-реестр\n"
        "/backup — собрать и прислать бэкап данных\n\n"
        "Записи с пометкой (.env) правятся только на сервере.\n"
        "🖥 Красивее — в приложении: кнопка ⚙️ в шапке формы."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выгрузка актуального xlsx-реестра админу в чат."""
    if not await _admin_gate(update):
        return
    from services.storage import registry_export_path

    path = registry_export_path()
    if path is None:
        await update.effective_message.reply_text(
            "Для Google-режима xlsx-выгрузка требует REGISTRY_XLSX_FILE в .env "
            "(либо откройте саму Google-таблицу)."
        )
        return
    if not path.exists():
        await update.effective_message.reply_text("Реестр пока пуст — ещё не было ни одной заявки.")
        return

    data = await asyncio.to_thread(path.read_bytes)
    await update.effective_message.reply_document(
        document=data,
        filename=path.name,
        caption=f"📊 Актуальный реестр · {max(len(data) // 1024, 1)} КБ",
    )
    user = update.effective_user
    await audit.log_event(
        audit.REGISTRY_EXPORTED,
        user.id if user else None,
        f"@{user.username}" if user and user.username else None,
        path.name,
    )


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной бэкап данных: собирает архив и присылает его админам."""
    if not await _admin_gate(update):
        return
    from services import backup

    await update.effective_message.reply_text("💾 Собираю бэкап…")
    try:
        path, delivered = await backup.run_backup(context.bot)
    except Exception:  # noqa: BLE001
        log.exception("Сбой ручного бэкапа")
        await update.effective_message.reply_text(
            "❌ Не удалось собрать бэкап — детали в логах."
        )
        return
    if delivered == 0:
        await update.effective_message.reply_text(
            f"Архив собран: {path.name} — но отправить файлом не вышло "
            "(слишком большой или чат недоступен), заберите с сервера."
        )


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Последние события аудит-журнала (кто подавал, кому отказано и т.п.)."""
    if not await _admin_gate(update):
        return
    events = await audit.recent_events(15)
    if not events:
        await update.effective_message.reply_text("🛡 Журнал безопасности пока пуст.")
        return
    e = html.escape
    lines = []
    for ev in events:
        who = ev["username"] or (str(ev["user_id"]) if ev["user_id"] else "—")
        line = f"<code>{ev['ts']}</code> · <b>{e(ev['event'])}</b> · {e(str(who))}"
        if ev["details"]:
            line += f"\n      {e(ev['details'])}"
        lines.append(line)
    await update.effective_message.reply_text(
        "🛡 <b>Журнал безопасности</b> (последние события)\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


def _arg(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    args = context.args or []
    return args[0].strip() if args else None


async def fin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_gate(update):
        return
    entry = _arg(context)
    if not entry:
        await update.effective_message.reply_text("Формат: /fin_add @username или /fin_add 12345")
        return
    if not rs.valid_financier_entry(entry):
        await update.effective_message.reply_text(
            "Некорректное значение: нужен числовой id или @username "
            "(5–32 символа: буквы, цифры, подчёркивание)."
        )
        return
    added = await asyncio.to_thread(rs.add_financier, entry)
    await update.effective_message.reply_text(
        f"✅ Финансист {entry} добавлен." if added else f"{entry} уже в списке."
    )


async def fin_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_gate(update):
        return
    entry = _arg(context)
    if not entry:
        await update.effective_message.reply_text("Формат: /fin_del @username или /fin_del 12345")
        return
    removed = await asyncio.to_thread(rs.remove_financier, entry)
    await update.effective_message.reply_text(
        f"🗑 Финансист {entry} убран."
        if removed
        else f"{entry} нет в динамическом списке (заданных в .env убрать можно только на сервере)."
    )


def _resolve_user_id(entry: str) -> int | None:
    """id из аргумента: число — как есть, @username — через справочник."""
    if entry.lstrip("-").isdigit():
        return int(entry)
    return resolve(entry)


async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_gate(update):
        return
    entry = _arg(context)
    if not entry:
        await update.effective_message.reply_text("Формат: /allow @username или /allow 12345")
        return
    uid = _resolve_user_id(entry)
    if uid is None:
        await update.effective_message.reply_text(
            f"Не знаю id пользователя {entry}: пусть напишет боту /start "
            "(или пришлите числовой id — его подскажет команда /myid)."
        )
        return
    added = await asyncio.to_thread(rs.add_allowed, uid)
    await update.effective_message.reply_text(
        f"✅ Доступ открыт: {entry} (id {uid})." if added else f"{entry} уже в whitelist."
    )


async def deny_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_gate(update):
        return
    entry = _arg(context)
    if not entry:
        await update.effective_message.reply_text("Формат: /deny @username или /deny 12345")
        return
    uid = _resolve_user_id(entry)
    if uid is None:
        await update.effective_message.reply_text(f"Не знаю id пользователя {entry}.")
        return
    removed = await asyncio.to_thread(rs.remove_allowed, uid)
    await update.effective_message.reply_text(
        f"🗑 Доступ закрыт: {entry} (id {uid})."
        if removed
        else f"{entry} нет в динамическом whitelist (записи из .env правятся на сервере)."
    )
