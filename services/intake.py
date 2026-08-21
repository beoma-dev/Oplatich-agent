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
from services import alerts, audit, dedup, storage
from services.notifier import notify_finance
from services.pdf_report import build_request_pdf

log = logging.getLogger(__name__)

# Статусы участника чата, при которых разрешаем публиковать итог в группу.
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


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
        # Потеря заявки — критично: алерт админам немедленно.
        await alerts.alert_admins(
            bot,
            "Заявка НЕ сохранилась в реестр",
            f"{request.request_id} от {request.sender_username} — детали в логах",
            signature="request-failed",
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

    await _send_user_confirmation(
        bot, request, pdf=pdf, notified=notified, file_warning=file_warning
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
) -> None:
    """Личное подтверждение автору — ОДНИМ сообщением: PDF с подписью."""
    e = html.escape
    if request.has_invoice:
        source_line = "Счёт сохранён в каталог «Счета на оплату»."
    else:
        source_line = "Счёта нет — оплата по указанным реквизитам."
    # Честная строка про финансиста: только если карточка реально доставлена.
    if notified > 0:
        urgent_note = (
            "\n🔴 Финансист уведомлён о срочности." if request.urgency.is_urgent else ""
        )
    elif request.urgency.is_urgent:
        urgent_note = (
            "\n⚠️ Срочная заявка, но уведомить финансиста не удалось — "
            "финансисты не настроены. Сообщите администратору."
        )
    else:
        urgent_note = ""
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
        f"{urgency_mark} · {source}"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        log.exception(
            "Не удалось отправить итог заявки %s в группу %s", request.request_id, chat_id
        )
