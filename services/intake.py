"""Финализация заявки: реестр → уведомления → подтверждения.

Единая точка для обоих каналов ввода — пошаговой чат-формы и Mini App.
"""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import Bot
from telegram.constants import ParseMode

from bot.models import InvoiceRequest
from bot.validators import has_profanity
from services import alerts, audit, dedup, storage, tg_retry
from services.notifier import notify_finance
from services.pdf_report import build_request_pdf
from services.runtime_settings import effective_finance_recipients

log = logging.getLogger(__name__)

# Статусы участника чата, при которых разрешаем публиковать итог в группу.
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


def _recovery_note(request: InvoiceRequest) -> str:
    """Что осталось от потерянной заявки — строками, годными для разбора."""
    planned = request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    lines = [
        f"{request.request_id} · {request.sender_username} ({request.sender_name})",
        f"{request.counterparty} — {request.amount:.2f} {request.currency}",
        f"Статья: {request.article or '—'} · оплата к {planned}",
        f"Счёт: {'приложен' if request.has_invoice else 'без счёта, по реквизитам'}",
    ]
    if request.comment:
        lines.append(f"Комментарий: {request.comment[:200]}")
    return "\n".join(lines)


async def finalize_submission(
    bot: Bot,
    request: InvoiceRequest,
    *,
    return_chat_id: int | None = None,
    invoice_file: bytes | None = None,
    file_warning: str | None = None,
) -> int:
    """Сохраняет заявку и рассылает уведомления. Возвращает номер записи.

    invoice_file — байты файла счёта (для вложения финансисту); при
    Google-бэкенде файла на диске нет, поэтому байты передаёт вызывающий.
    Ошибка записи в реестр пробрасывается наверх — заявка НЕ принята.
    Ошибки уведомлений только логируются: заявка уже сохранена, и пользователь
    не должен получить ложное «не удалось сохранить».
    """
    try:
        row_number = await storage.append_invoice(request)
    except Exception:
        await audit.log_event(
            audit.REQUEST_FAILED,
            request.telegram_id,
            request.sender_username,
            f"{request.request_id}: ошибка записи в реестр",
        )
        # Потеря заявки — критично: алерт админам немедленно, и в нём —
        # содержимое заявки. Раньше стояло «детали в логах», но по инварианту
        # проекта суммы и реквизиты в логи не пишутся: восстанавливать было
        # не из чего. Реквизиты не выносим и сюда — для разбора хватает того,
        # что ниже, а сообщение живёт в переписке дольше, чем нужно.
        await alerts.alert_admins(
            bot,
            "Заявка НЕ сохранилась в реестр",
            _recovery_note(request),
            signature="request-failed",
            kind="storage",
            hint=(
                "Заявка отклонена и автору не видна. Проверьте доступность "
                "реестра, затем попросите подать её заново."
            ),
        )
        raise

    await audit.log_event(
        audit.REQUEST_SUBMITTED,
        request.telegram_id,
        request.sender_username,
        f"{request.request_id} · {request.amount} {request.currency} · {request.counterparty}",
    )
    # Отпечаток для защиты от повторной подачи того же счёта.
    await dedup.remember(request)

    if file_warning:
        await audit.log_event(
            audit.FILE_SUSPICIOUS,
            request.telegram_id,
            request.sender_username,
            f"{request.request_id} · {request.file_name}",
        )

    # PDF-документ заявки: сбой генерации не срывает сценарий (заявка в реестре).
    pdf: bytes | None = None
    try:
        pdf = await asyncio.to_thread(build_request_pdf, request)
        await storage.save_artifact(pdf, f"{request.request_id}.pdf")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сформировать PDF по заявке %s", request.request_id)

    notified = 0
    try:
        notified = await notify_finance(
            bot,
            request,
            row_number,
            pdf=pdf,
            invoice_file=invoice_file,
            file_warning=file_warning,
        )
    except Exception:  # noqa: BLE001
        log.exception("Сбой уведомления финансистов по заявке %s", request.request_id)

    # Мат не блокируем: запрет провоцирует обходы и ложные срабатывания,
    # а заявка неанонимна — в реестре, карточке и PDF стоит имя автора.
    # Поэтому просто говорим админу: дальше это разговор, а не техника.
    profane = [
        label
        for label, value in (
            ("контрагент", request.counterparty),
            ("статья", request.article),
            ("срок работ", request.work_deadline),
            ("комментарий", request.comment),
        )
        if has_profanity(value)
    ]
    if profane:
        try:
            await alerts.alert_admins(
                bot,
                "Мат в заявке",
                f"{request.request_id} от {request.sender_username} "
                f"({request.sender_name}): {', '.join(profane)}.",
                signature=f"profanity-{request.telegram_id}",
                kind="moderation",
            )
        except Exception:  # noqa: BLE001 — алерт не должен ломать подачу
            log.exception("Сбой алерта о мате в заявке %s", request.request_id)

    if notified == 0:
        # Тишины быть не должно: заявка записана, а карточку никто не увидел,
        # и переслать её потом нечем. Раньше об этом знал только лог — так и
        # потерялась заявка при двухминутном провале WARP. Сообщаем АДМИНУ,
        # а не автору: чинить это ему, а не сотруднику.
        if effective_finance_recipients():
            title = "Карточка заявки не дошла ни одному финансисту"
            details = (
                f"{request.request_id}: {request.amount:.2f} {request.currency}, "
                f"{request.counterparty}. Заявка в реестре есть, карточки нет — "
                "статус можно поставить из панели финансиста."
            )
        else:
            title = "Финансисты не настроены"
            details = (
                f"{request.request_id}: заявка записана, но получателей карточки "
                "нет. Добавьте финансиста в админ-панели ⚙️."
            )
        try:
            await alerts.alert_admins(
                bot, title, details,
                signature="finance-undelivered",
                kind="delivery",
                hint=(
                    "Заявка в реестре есть, а карточки у финансиста нет — "
                    "передайте её вручную или попросите его начать чат с ботом."
                ),
            )
        except Exception:  # noqa: BLE001 — алерт не должен ломать подачу
            log.exception("Сбой алерта о недоставленной карточке")

    # Заявка подана из группы — итог уже уходит туда, и личное подтверждение
    # автору было бы ровно тем же текстом второй раз. Дублировать не нужно,
    # но и молчать нельзя: предупреждение автопроверки счёта и осечку с
    # уведомлением финансиста в групповую сводку не пишут (в ней намеренно
    # нет ничего чувствительного), поэтому их автор получает отдельно.
    await _send_user_confirmation(
        bot,
        request,
        pdf=pdf,
        notified=notified,
        file_warning=file_warning,
        summary_in_group=return_chat_id is not None,
    )

    if return_chat_id is not None:
        await _post_group_summary(bot, request, return_chat_id)

    return row_number


async def _send_user_confirmation(
    bot: Bot,
    request: InvoiceRequest,
    pdf: bytes | None = None,
    notified: int = 0,
    file_warning: str | None = None,
    summary_in_group: bool = False,
) -> None:
    """Личное подтверждение автору — ОДНИМ сообщением: PDF с подписью.

    `summary_in_group=True` — итог уже опубликован в группе, второй раз тем
    же текстом автору не пишем. Молчим ТОЛЬКО когда сказать нечего: если
    есть предупреждение по файлу или срочную заявку не удалось донести до
    финансиста, автор об этом узнает — в групповую сводку это не попадает.
    """
    e = html.escape
    if request.has_invoice:
        source_line = "Счёт сохранён в каталог «Счета на оплату»."
    else:
        source_line = "Счёта нет — оплата по указанным реквизитам."
    # Про финансиста автору сообщаем только хорошее: что срочную заявку
    # реально доставили. Об осечке узнаёт АДМИН отдельным алертом — сотрудник
    # с ней всё равно ничего не сделает, а «сообщите администратору» в чужих
    # руках превращается в тревогу без действия.
    urgent_note = (
        "\n🔴 Финансист уведомлён о срочности."
        if notified > 0 and request.urgency.is_urgent
        else ""
    )
    planned = request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    text = (
        f"✅ <b>Заявка принята</b>\n"
        f"Номер: {e(request.request_id)}\n"
        f"Сумма: {e(f'{request.amount:,.2f}')} {e(request.currency)}\n"
        f"Контрагент: {e(request.counterparty)}\n"
        f"📅 Оплатить до: <b>{e(planned)}</b>\n"
        f"{source_line}" + urgent_note
    )
    if file_warning:
        text += f"\n{e(file_warning)}"
    if summary_in_group:
        # Оставляем только то, чего в групповой сводке нет.
        extras = [part for part in (urgent_note.strip(), e(file_warning) if file_warning else "") if part]
        if not extras:
            return
        text = "\n".join([f"✅ Заявка {e(request.request_id)} принята.", *extras])
        pdf = None
    try:
        if pdf is not None:
            await bot.send_document(
                chat_id=request.telegram_id,
                document=pdf,
                filename=f"{request.request_id}.pdf",
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=request.telegram_id, text=text, parse_mode=ParseMode.HTML
            )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить подтверждение по заявке %s", request.request_id)


async def _user_in_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, что автор заявки состоит в чате (защита от подделки chat_id)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in _MEMBER_STATUSES
    except Exception:  # noqa: BLE001 — бот не в чате, чата нет и т.п.
        return False


async def _post_group_summary(bot: Bot, request: InvoiceRequest, chat_id: int) -> None:
    """Публикует в группе краткое уведомление о созданной заявке.

    Итог публикуется только если автор заявки — участник этой группы:
    return_chat_id приходит из deep-link/URL и может быть подделан.
    """
    if not await _user_in_chat(bot, chat_id, request.telegram_id):
        log.warning(
            "Итог заявки %s не отправлен в чат %s: автор не участник чата",
            request.request_id, chat_id,
        )
        await audit.log_event(
            audit.GROUP_POST_REJECTED,
            request.telegram_id,
            request.sender_username,
            f"{request.request_id}: return_chat={chat_id}",
        )
        return

    e = html.escape
    urgency_mark = "🔴 Срочно" if request.urgency.is_urgent else "🟢 Обычная"
    source = "📎 со счётом" if request.has_invoice else "✍️ по реквизитам"
    planned = request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    text = (
        "✅ <b>Создана заявка на оплату</b>\n"
        f"№ {e(request.request_id)}\n"
        f"От: {e(request.sender_username)} ({e(request.sender_name)})\n"
        f"Сумма: <b>{e(f'{request.amount:,.2f}')} {e(request.currency)}</b>\n"
        f"Контрагент: {e(request.counterparty)}\n"
        f"📂 Статья: {e(request.article or '—')}\n"
        f"📄 Срок работ: {e(request.work_deadline or '—')}\n"
        f"📅 Срок исполнения: <b>{e(planned)}</b>\n"
        f"💬 Комментарий: {e(request.comment or '—')}\n"
        f"{urgency_mark} · {source}"
    )
    try:
        await tg_retry.send_with_retry(
            lambda: bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            ),
            what=f"Подтверждение автору {chat_id}",
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "Не удалось отправить итог заявки %s в группу %s", request.request_id, chat_id
        )
