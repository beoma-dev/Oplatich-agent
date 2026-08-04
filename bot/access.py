"""Контроль доступа: whitelist подачи заявок и права админа бота."""
from __future__ import annotations

import logging
import time

from telegram import Bot
from telegram.constants import ChatMemberStatus

from config import settings
from services.runtime_settings import admin_chat_ids, effective_allowed_ids

log = logging.getLogger(__name__)

# Кэш проверки «админ ли чата»: не дёргаем getChatMember на каждый клик.
_ADMIN_CACHE_TTL = 300.0
_admin_cache: dict[int, tuple[bool, float]] = {}


def is_allowed(user_id: int) -> bool:
    """True, если пользователю разрешено подавать заявки.

    Whitelist = ALLOWED_USER_IDS из .env + добавленные админом из Telegram.
    FAIL-CLOSED: пустой список = не пускать НИКОГО. Для финансового бота
    «забыли настроить» должно означать «закрыто», а не «открыто всем».
    Админы бота проходят всегда — иначе свежую установку невозможно
    настроить и проверить.
    """
    allowed = effective_allowed_ids()
    if user_id in settings.admin_ids:
        return True
    if not allowed:
        log.warning("Whitelist пуст — доступ закрыт (fail-closed), user_id=%s", user_id)
        return False
    return user_id in allowed


def is_admin(user_id: int) -> bool:
    """Статическая часть прав админа: ADMIN_IDS из .env (корневые админы)."""
    return user_id in settings.admin_ids


def is_financier(user_id: int) -> bool:
    """True — этому человеку уходят карточки заявок (он же видит панель).

    Список финансистов = FINANCE_CHAT_IDS из .env + добавленные админом;
    @username приводятся к id справочником. Импорт локальный: notifier
    тянет за собой пол-сервисного слоя, а access читают из хендлеров.
    """
    from services.notifier import resolved_finance_ids

    return user_id in resolved_finance_ids()


async def is_bot_admin(bot: Bot, user_id: int) -> bool:
    """Полная проверка прав админа бота.

    Админ = из ADMIN_IDS (.env) ИЛИ администратор/владелец доверенного чата
    (канала/группы, куда бота добавил существующий админ). Результат
    кэшируется на 5 минут — разжалованный админ канала теряет права
    с этой задержкой.
    """
    if is_admin(user_id):
        return True

    cached = _admin_cache.get(user_id)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]

    result = False
    for chat_id in admin_chat_ids():
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except Exception:  # noqa: BLE001 — бот удалён из чата и т.п.
            continue
        if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            result = True
            break
    _admin_cache[user_id] = (result, now + _ADMIN_CACHE_TTL)
    return result


def access_denied_message() -> str:
    return (
        "⛔ У вас нет доступа к подаче заявок на оплату.\n"
        "Обратитесь к администратору, чтобы вас добавили в список сотрудников "
        "(админ: ⚙️ в форме или /allow, ваш id — /myid)."
    )
