"""Уведомления финансистам: карточка каждой заявки с кнопками статуса.

Карточка уходит по КАЖДОЙ заявке (срочные помечены 🔴), с вложениями:
PDF-документ заявки и сам файл счёта. Кнопки «Оплачено/Отложено/Отклонено»
обрабатываются в bot/finance_actions.py.
"""
from __future__ import annotations

import html
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from bot.models import REQUEST_STATUSES, InvoiceRequest
from services import cards, tg_retry
from services import runtime_settings as rs
from services.runtime_settings import effective_finance_recipients
from services.user_directory import resolve

log = logging.getLogger(__name__)

# Префикс callback_data кнопок статуса: ST:<request_id>:<KEY>.
CB_STATUS_PREFIX = "ST"


async def _send_with_retry(send, chat_id: int):
    """Карточка финансисту с повтором на сетевых сбоях.

    Механизм общий (services/tg_retry): заявка уже записана в реестр, а
    карточку никто не перешлёт — потерять её из-за одного сетевого сбоя
    нельзя. Бюджет пауз короткий: подача заявки ждёт этой отправки.
    """
    return await tg_retry.send_with_retry(send, what=f"Финансист {chat_id}")


def build_status_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Кнопки смены статуса под карточкой финансиста."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"{CB_STATUS_PREFIX}:{request_id}:{key}")
        for key, (label, _status) in REQUEST_STATUSES.items()
    ]])


def resolved_finance_ids() -> list[int]:
    """Финансисты (.env + добавленные админом), приведённые к chat_id (без дублей)."""
    chat_ids: list[int] = []
    for entry in effective_finance_recipients():
        rid = resolve(entry)
        if rid is None:
            log.warning(
                "Финансист %s ещё не известен боту (не писал боту / не появлялся в группе)",
                entry,
            )
            continue
        if rid not in chat_ids:
            chat_ids.append(rid)
    return chat_ids


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_card(request: InvoiceRequest, row_number: int) -> str:
    """Карточка заявки для финансиста (HTML).

    Идёт ПОДПИСЬЮ к документу (лимит Telegram — 1024 символа), поэтому
    длинные комментарий и реквизиты обрезаются: полные значения есть
    в PDF-документе и в реестре.
    """
    e = html.escape
    header = (
        "🔴 <b>СРОЧНАЯ оплата</b>" if request.urgency.is_urgent
        else "🧾 <b>Новая заявка на оплату</b>"
    )
    if request.has_invoice:
        source_part = "\n📎 Счёт — этим файлом."
    elif request.requisites:
        # tg-spoiler: реквизиты скрыты до нажатия (как ||спойлер|| в Telegram).
        source_part = (
            f"\n✍️ <b>Без счёта.</b> Реквизиты — нажмите, чтобы показать:\n"
            f"<tg-spoiler>{e(_clip(request.requisites, 300))}</tg-spoiler>"
        )
    else:
        # Ни счёта, ни реквизитов — с 26.08.2026 это законная заявка, но
        # финансисту платить по ней не по чему, и молчать об этом нельзя:
        # пустая строка выглядела бы как «карточка обрезалась».
        source_part = "\n⚠️ <b>Ни счёта, ни реквизитов.</b> Уточните у автора."
    planned = (
        request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    )
    # Дополнительные документы: ссылками, а не вложениями. Подпись к документу
    # ограничена 1024 символами, и пять файлов в одном сообщении её съедят;
    # к тому же вложение можно отправить только одно — им идёт счёт.
    extras_part = ""
    if request.extra_files:
        links = "\n".join(
            f'  <a href="{e(url)}">документ {i}</a>'
            for i, url in enumerate(request.extra_files, start=1)
        )
        extras_part = f"\n📁 Ещё документов: {len(request.extra_files)}\n{links}"
    return (
        f"{header}\n\n"
        f"💰 Сумма: <b>{e(f'{request.amount:,.2f}')} {e(request.currency)}</b>\n"
        f"🏢 Контрагент: {e(_clip(request.counterparty, 120))}\n"
        f"📂 Статья: {e(_clip(request.article or '—', 80))}\n"
        f"📄 Срок работ: {e(_clip(request.work_deadline or '—', 80))}\n"
        f"📅 Оплатить до: <b>{e(planned)}</b>\n"
        f"👤 Отправитель: {e(request.sender_username)} ({e(request.sender_name)})\n"
        f"📝 Комментарий: {e(_clip(request.comment, 250) or '—')}\n"
        f"🧾 Заявка: {e(request.request_id)} (строка {row_number})"
        f"{source_part}{extras_part}"
    )


def recipients_for(request: InvoiceRequest) -> list[int]:
    """Кому эта заявка уйдёт карточкой — с учётом личного фильтра срочности.

    Финансист может попросить присылать ему только срочные: обычных заявок
    в день бывает много, и уведомление о каждой перестаёт читаться. Срочные
    приходят всем всегда — на то они и срочные, отписаться от них нельзя.

    Отдельная функция, а не условие внутри рассылки: по ней же вызывающий
    отличает «никому не отправили» от «никому и не собирались». Первое —
    сбой, о котором будят админа; второе — осознанный выбор получателей.
    """
    ids = resolved_finance_ids()
    if request.urgency.is_urgent:
        return ids
    return [
        chat_id for chat_id in ids
        if rs.personal_card_urgency(chat_id) == rs.CARD_URGENCY_ALL
    ]


def suppressed_by_choice(request: InvoiceRequest) -> bool:
    """Карточки нет ПО ВЫБОРУ получателей, а не из-за сбоя.

    Истинно только когда получатели есть, но все они просили присылать им
    лишь срочные, а заявка обычная. Пустой список финансистов сюда НЕ
    относится: это настоящая дыра, и о ней админа будить надо.
    """
    return bool(resolved_finance_ids()) and not recipients_for(request)


async def notify_finance(
    bot: Bot,
    request: InvoiceRequest,
    row_number: int,
    pdf: bytes | None = None,
    invoice_file: bytes | None = None,
    file_warning: str | None = None,
) -> int:
    """Рассылает карточку заявки всем финансистам ОДНИМ сообщением.

    Карточка — подпись к документу с кнопками статуса: приложен файл счёта,
    а для заявок по реквизитам — PDF-документ заявки. Отдельные сообщения
    с PDF и файлом не шлются — у финансиста один аккуратный блок.

    Возвращает число получателей, которым карточка реально доставлена, —
    по нему подтверждение автору честно сообщает, уведомлён ли финансист.
    Ошибка отправки одному получателю не срывает остальных и не срывает
    основной сценарий (заявка уже сохранена).
    """
    if not effective_finance_recipients():
        if request.urgency.is_urgent:
            log.warning("Срочная заявка %s, но финансисты не настроены", request.request_id)
        return 0

    chat_ids = recipients_for(request)
    if not chat_ids:
        if resolved_finance_ids():
            # Не сбой: обычную заявку никто не захотел получать карточкой.
            # Она в реестре и видна в панели — будить админа незачем.
            log.info(
                "Заявка %s: все получатели просили только срочные — карточка не идёт",
                request.request_id,
            )
        else:
            log.warning(
                "Заявка %s: нет ни одного резолвнутого финансиста", request.request_id
            )
        return 0

    delivered = 0
    text = _format_card(request, row_number)
    if file_warning:
        text += f"\n{html.escape(file_warning)}"
    keyboard = build_status_keyboard(request.request_id)
    # Приоритет вложения: файл счёта (нужен для оплаты); без счёта — PDF
    # заявки (там реквизиты целиком и сумма прописью).
    if invoice_file is not None:
        document, filename = invoice_file, (request.file_name or "invoice")
    elif pdf is not None:
        document, filename = pdf, f"{request.request_id}.pdf"
    else:
        document, filename = None, ""

    for chat_id in chat_ids:
        try:
            if document is not None:
                message = await _send_with_retry(
                    lambda cid=chat_id: bot.send_document(
                        chat_id=cid,
                        document=document,
                        filename=filename,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    ),
                    chat_id,
                )
            else:
                message = await _send_with_retry(
                    lambda cid=chat_id: bot.send_message(
                        chat_id=cid,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    ),
                    chat_id,
                )
            delivered += 1
            # Запоминаем карточку: при смене статуса обновим её у ВСЕХ.
            await cards.save(
                request.request_id,
                chat_id,
                message.message_id,
                is_caption=document is not None,
                base_html=text,
            )
            log.info("Заявка %s отправлена финансисту %s", request.request_id, chat_id)
        except Exception:  # noqa: BLE001 — логируем и продолжаем
            log.exception("Не удалось уведомить финансиста %s", chat_id)
    return delivered
