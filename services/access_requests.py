"""Запрос доступа к подаче заявок: сотрудник просит — админы решают.

Whitelist работает fail-closed, поэтому новый человек упирается в отказ и
не знает, к кому идти. Здесь он нажимает одну кнопку, а админы получают
карточку с решением. Точка входа одна для обоих каналов — чат-формы и
Mini App, чтобы поведение не разъезжалось.
"""
from __future__ import annotations

import html
import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from services import audit
from services import runtime_settings as rs

log = logging.getLogger(__name__)

# callback_data: ACQ:<ok|no>:<user_id>
CB_PREFIX = "ACQ"
CB_APPROVE = f"{CB_PREFIX}:ok"
CB_REJECT = f"{CB_PREFIX}:no"
# Кнопка «попросить доступ» из чата, где id берётся из самого апдейта.
CB_ASK = f"{CB_PREFIX}:ask"

ALREADY_PENDING = "⏳ Заявка уже отправлена — админы её видят, ждите ответа."
NO_ADMINS = (
    "⚠️ Некому рассмотреть заявку: у бота не задан ни один админ. "
    "Обратитесь к тому, кто его настраивал."
)
SENT = "✅ Заявка отправлена админам. Ответ придёт сюда же."


def _who(user_id: int, username: str, full_name: str) -> str:
    """Как показать человека админу: @username, имя и обязательно id."""
    parts = []
    if username:
        parts.append("@" + username.lstrip("@"))
    if full_name and full_name != username:
        parts.append(full_name)
    parts.append(f"id {user_id}")
    return " · ".join(parts)


async def request_access(
    bot: Bot, user_id: int, username: str = "", full_name: str = ""
) -> str:
    """Регистрирует просьбу и рассылает её админам. Возвращает текст для автора."""
    admins = rs.effective_admin_ids()
    if not admins:
        return NO_ADMINS
    if not rs.add_access_request(user_id, username, time.time()):
        return ALREADY_PENDING

    who = _who(user_id, username, full_name)
    text = (
        "🔑 <b>Просят доступ к подаче заявок</b>\n"
        f"{html.escape(who)}\n\n"
        "Открыть доступ — человек сможет подавать заявки на оплату."
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Открыть доступ", callback_data=f"{CB_APPROVE}:{user_id}"),
        InlineKeyboardButton("🚫 Отказать", callback_data=f"{CB_REJECT}:{user_id}"),
    ]])
    delivered = 0
    for admin_id in admins:
        try:
            await bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — недоступный админ не ломает заявку
            log.warning("Заявка на доступ не доставлена админу %s", admin_id)
    await audit.log_event(audit.ACCESS_REQUESTED, user_id, username or None, who)
    if not delivered:
        # Заявку не снимаем: админ увидит её в панели, когда откроет чат.
        log.warning("Заявка на доступ %s не дошла ни до одного админа", user_id)
    return SENT


async def resolve_access(
    bot: Bot, target_id: int, approve: bool, *, actor_id: int, actor_name: str
) -> str:
    """Решение админа. Возвращает короткий итог для карточки в чате."""
    rs.clear_access_request(target_id)
    if approve:
        rs.add_allowed(target_id)
        note = f"✅ Доступ открыт (id {target_id})"
        to_user = (
            "✅ Доступ открыт — можно подавать заявки на оплату.\n"
            "Нажмите /start, чтобы начать."
        )
    else:
        note = f"🚫 Отказано (id {target_id})"
        to_user = "🚫 В доступе отказано. Уточните у администратора, почему."
    try:
        await bot.send_message(chat_id=target_id, text=to_user)
    except Exception:  # noqa: BLE001 — личка закрыта, решение всё равно в силе
        log.info("Ответ по доступу не доставлен пользователю %s", target_id)
    await audit.log_event(
        audit.ACCESS_RESOLVED, actor_id, actor_name, f"{'approve' if approve else 'reject'} {target_id}"
    )
    return note
