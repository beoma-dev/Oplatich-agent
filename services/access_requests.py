"""Запрос доступа к подаче заявок: сотрудник просит — админы решают.

Whitelist работает fail-closed, поэтому новый человек упирается в отказ и
не знает, к кому идти. Здесь он нажимает одну кнопку, а админы получают
карточку с решением. Точка входа одна для обоих каналов — чат-формы и
Mini App, чтобы поведение не разъезжалось.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import settings
from services import access_cards, audit
from services import runtime_settings as rs
from services.user_directory import remember, username_for

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
    # Запоминаем @username сразу: whitelist хранит числовые id (ник человек
    # может сменить), а показывает справочник. Из Mini App апдейта в чат нет,
    # и без этой записи выданный доступ выглядел бы как «id 930027909».
    if username:
        await asyncio.to_thread(remember, user_id, username)

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
            msg = await bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — недоступный админ не ломает заявку
            log.warning("Заявка на доступ не доставлена админу %s", admin_id)
            continue
        # Где лежит карточка — чтобы закрыть её у ВСЕХ, а не только у того,
        # кто нажал. Номер сообщения бывает не числом (заглушка бота в
        # тестах); тогда просто не запоминаем — заявка от этого не страдает.
        message_id = getattr(msg, "message_id", None)
        if isinstance(message_id, int):
            await access_cards.save(user_id, admin_id, message_id, text)
    await audit.log_event(audit.ACCESS_REQUESTED, user_id, username or None, who)
    if not delivered:
        # Заявку не снимаем: админ увидит её в панели, когда откроет чат.
        log.warning("Заявка на доступ %s не дошла ни до одного админа", user_id)
    return SENT


async def _close_cards(
    bot: Bot,
    target_id: int,
    note: str,
    actor_name: str,
    fallback_card: dict | None,
) -> None:
    """Дописывает итог во ВСЕ карточки заявки и снимает с них кнопки.

    Иначе у остальных админов остаётся живая кнопка по решённому вопросу.
    Нажатие на неё теперь безвредно (см. resolve_access), но человек всё
    равно жмёт и не понимает, почему ничего не происходит, — а карточки
    живут в переписке месяцами.

    fallback_card — та, на которой нажали: страховка для заявок, разосланных
    до появления таблицы карточек. Сбой одной карточки не срывает остальные:
    сообщение могли удалить, а решение уже принято и отменять его нечем.
    """
    cards = await access_cards.for_target(target_id)
    if not cards and fallback_card:
        cards = [fallback_card]
    tail = f"\n\n<b>{html.escape(note)}</b>"
    if actor_name:
        # Подписываем только СВОЁ решение. У запоздалого нажатия решал другой
        # человек и раньше: поставить тут нажавшего и время нажатия значило бы
        # соврать в той самой карточке, которая и должна прояснить, что было.
        stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
        tail += f" · {html.escape(actor_name)} · {stamp}"
    for card in cards:
        try:
            await bot.edit_message_text(
                chat_id=card["chat_id"],
                message_id=card["message_id"],
                text=(card.get("base_html") or "") + tail,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:  # noqa: BLE001 — карточки могло уже не быть
            log.info(
                "Карточка заявки на доступ %s в чате %s не обновилась",
                target_id, card["chat_id"],
            )
    await access_cards.clear_target(target_id)


async def settle_elsewhere(bot: Bot, target_id: int, opened: bool, actor_name: str) -> bool:
    """Заявку закрыли не кнопкой на карточке, а панелью или командой.

    Доступ выдают тремя дверями: кнопка на карточке, ⚙️ → Доступ и /allow.
    Снимала висящую заявку только первая, и от этого карточки оставались
    живыми: человеку уже открыли доступ из панели, а другой админ потом
    жал на своей карточке «Отказать» — заявка снималась как нерешённая,
    человек получал «в доступе отказано», а доступ у него оставался.
    Идемпотентность resolve_access эту дверь не закрывает: заявка-то ещё
    висела, и нажатие выглядело первым.

    True — заявка висела и снята нами (значит, карточки были и закрыты).
    """
    if not rs.clear_access_request(target_id):
        return False
    who = username_for(target_id) or f"id {target_id}"
    note = f"✅ Доступ открыт: {who}" if opened else f"🚫 Доступ закрыт: {who}"
    await _close_cards(bot, target_id, note, actor_name, None)
    return True


async def resolve_access(
    bot: Bot,
    target_id: int,
    approve: bool,
    *,
    actor_id: int,
    actor_name: str,
    fallback_card: dict | None = None,
) -> tuple[bool, str]:
    """Решение админа: (применено ли, короткий итог для карточки).

    Заявку видят ВСЕ админы, а снять её может только один — и признак
    победителя в гонке возвращает сам `clear_access_request`. Раньше это
    значение выбрасывалось, и запоздалое нажатие проходило как полноценное
    решение. Проверено прогоном 30.09.2026: «Отказать» после чужого
    «Открыть доступ» оставляло доступ ОТКРЫТЫМ (отзыва тут нет и не надо —
    в норме отзывать нечего), писало человеку «в доступе отказано» и
    клало в аудит отказ. Расходились три картины мира разом: у человека,
    у второго админа и в журнале.

    Побеждает ПЕРВОЕ решение. Отозвать выданный доступ можно явно, в панели
    (⚙️ → Доступ), а не случайным касанием по карточке месячной давности.
    """
    # Ник знаем из справочника — в итоге на карточке он читается лучше id.
    who = username_for(target_id) or f"id {target_id}"
    if not rs.clear_access_request(target_id):
        opened = target_id in rs.effective_allowed_ids()
        outcome = "доступ уже открыт" if opened else "решение уже принято"
        # Отдельным событием, а не ACCESS_RESOLVED: запоздалое нажатие —
        # это не решение, и в журнале «кто что решил» ему не место. Старые
        # записи отличались бы только формой details, а этот урок в проекте
        # уже оплачен (закрывающие документы считались подачей заявки).
        await audit.log_event(
            audit.ACCESS_LATE_CLICK,
            actor_id,
            actor_name,
            f"{'approve' if approve else 'reject'} {target_id} · заявка уже закрыта",
        )
        late = f"⏳ Заявку уже рассмотрели — {outcome} ({who})."
        await _close_cards(bot, target_id, late, "", fallback_card)
        return False, late

    if approve:
        rs.add_allowed(target_id)
        note = f"✅ Доступ открыт: {who}"
        to_user = (
            "✅ Доступ открыт — можно подавать заявки на оплату.\n"
            "Нажмите /start, чтобы начать."
        )
    else:
        note = f"🚫 Отказано: {who}"
        to_user = "🚫 В доступе отказано. Уточните у администратора, почему."
    try:
        await bot.send_message(chat_id=target_id, text=to_user)
    except Exception:  # noqa: BLE001 — личка закрыта, решение всё равно в силе
        log.info("Ответ по доступу не доставлен пользователю %s", target_id)
    await audit.log_event(
        audit.ACCESS_RESOLVED, actor_id, actor_name, f"{'approve' if approve else 'reject'} {target_id}"
    )
    await _close_cards(bot, target_id, note, actor_name, fallback_card)
    return True, note
