"""Смена статуса заявки — общая точка для карточки в чате и панели Mini App.

Один сценарий на оба канала: статус в реестр → карточки у ВСЕХ финансистов →
причина в пометки → аудит → уведомление автору. Иначе кнопка на карточке и
кнопка в панели неизбежно разъехались бы в поведении.

Отозванную заявку не трогаем: её автор забрал, а карточка могла остаться
открытой в чужом чате.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode

from bot.models import REQUEST_STATUSES, STATUS_WITHDRAWN
from config import settings
from services import audit, cards, request_meta, storage, tg_retry
from services.notifier import build_status_keyboard, miniapp_link

log = logging.getLogger(__name__)


async def apply_status(
    bot: Bot,
    request_id: str,
    key: str,
    *,
    actor_id: int,
    actor_name: str,
    reason: str | None = None,
    fallback_card: dict | None = None,
) -> tuple[bool, str]:
    """Ставит статус заявке. Возвращает (получилось ли, сообщение для UI)."""
    entry = REQUEST_STATUSES.get(key)
    if entry is None:
        return False, "Неизвестный статус."
    label, status_text = entry

    current = await storage.get_request(request_id)
    if current is None:
        return False, "Заявка не найдена в реестре."
    if current.get("Статус оплаты", "") == STATUS_WITHDRAWN:
        return False, "Заявка отозвана автором — статус менять нельзя."

    row = await storage.set_request_status(request_id, status_text)
    if row is None:
        return False, "Не удалось обновить статус заявки."

    e = html.escape
    now_s = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    status_line = f"{cards.STATUS_MARK} {label}</b> · {e(actor_name)} · {now_s}"
    if reason:
        status_line += f"\n💬 Причина: {e(reason)}"

    await cards.update_all(
        bot,
        request_id,
        status_line,
        keyboard=build_status_keyboard(request_id, miniapp_link(bot, request_id)),
        fallback=fallback_card,
    )

    # Причина нужна автору и потом — в «Моих заявках», даже если пуш не дошёл.
    if reason:
        await request_meta.save_reason(request_id, status_text, reason, actor_name)

    await audit.log_event(
        audit.STATUS_CHANGED,
        actor_id,
        actor_name,
        f"{request_id} → {status_text}" + (f" · причина: {reason}" if reason else ""),
    )
    await notify_author(bot, row, request_id, label, reason)
    return True, f"Статус: {status_text}"


async def notify_author(
    bot: Bot, row: dict[str, str], request_id: str, label: str, reason: str | None
) -> None:
    """Сообщает автору заявки о смене статуса (с причиной, если указана)."""
    try:
        author_id = int(row.get("Telegram ID", ""))
    except ValueError:
        log.warning("Заявка %s: не удалось определить автора для уведомления", request_id)
        return

    e = html.escape
    text = (
        "📌 <b>Статус вашей заявки обновлён</b>\n"
        f"№ {e(request_id)}\n"
        f"Контрагент: {e(row.get('Контрагент', '—'))}\n"
        f"Сумма: {e(row.get('Сумма', '—'))} {e(row.get('Валюта', ''))}\n"
        f"📅 Оплатить до: {e(row.get('Плановая дата оплаты', '—') or '—')}\n"
        f"📄 Срок работ: "
        f"{e(row.get('Срок исполнения работ по договору', '—') or '—')}\n"
        f"Новый статус: <b>{label}</b>"
    )
    if reason:
        text += f"\n💬 Причина: {e(reason)}"
    try:
        # С повтором: раньше сбой глушился целиком, а комментарий объяснял
        # его закрытой личкой. При потерях на канале (reports/005) заметная
        # доля этих «личек» была сетью, и автор просто не узнавал, что его
        # заявку оплатили.
        await tg_retry.send_with_retry(
            lambda: bot.send_message(
                chat_id=author_id, text=text, parse_mode=ParseMode.HTML
            ),
            what=f"Автор заявки {request_id}",
        )
    except Exception:  # noqa: BLE001 — автор мог не открывать личку с ботом
        log.warning("Не удалось уведомить автора заявки %s (id %s)", request_id, author_id)
