"""Карточки финансистов: где лежит каждая разосланная карточка заявки.

Хранится (chat_id, message_id, caption/text, базовый HTML без строки статуса),
чтобы при смене статуса обновлять карточки у ВСЕХ финансистов, а не только
у нажавшего, — иначе можно «оплатить» уже отклонённую заявку по чужой
неактуальной карточке.

Та же SQLite, что аудит/дедуп/реестр. Синхронно — через to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import settings

log = logging.getLogger(__name__)

# Маркер строки статуса в карточке — по нему же отрезаем прежний статус.
STATUS_MARK = "\n\n📌 <b>Статус:"


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_cards (
            request_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            is_caption INTEGER NOT NULL,
            base_html TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cards_request ON finance_cards (request_id)"
    )
    return conn


def save_sync(
    request_id: str, chat_id: int, message_id: int, is_caption: bool, base_html: str
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO finance_cards "
            "(request_id, chat_id, message_id, is_caption, base_html) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, chat_id, message_id, int(is_caption), base_html),
        )


def for_request_sync(request_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, message_id, is_caption, base_html "
            "FROM finance_cards WHERE request_id = ?",
            (request_id,),
        ).fetchall()
    return [
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "is_caption": bool(is_caption),
            "base_html": base_html,
        }
        for chat_id, message_id, is_caption, base_html in rows
    ]


async def save(
    request_id: str, chat_id: int, message_id: int, is_caption: bool, base_html: str
) -> None:
    """Запоминает карточку; сбой не срывает рассылку."""
    try:
        await asyncio.to_thread(
            save_sync, request_id, chat_id, message_id, is_caption, base_html
        )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сохранить карточку заявки %s", request_id)


async def for_request(request_id: str) -> list[dict]:
    try:
        return await asyncio.to_thread(for_request_sync, request_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать карточки заявки %s", request_id)
        return []


async def update_all(
    bot: Bot,
    request_id: str,
    status_line: str,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
    fallback: dict | None = None,
) -> int:
    """Дописывает строку статуса во ВСЕ карточки заявки. Возвращает число обновлённых.

    keyboard=None убирает кнопки — так закрывается карточка отозванной заявки.
    fallback — карточка, на которой нажали: резерв для заявок, разосланных до
    появления таблицы карточек. Сбой одной карточки не срывает остальные:
    сообщение могло быть удалено или уже содержать тот же текст.
    """
    card_list = await for_request(request_id)
    if not card_list and fallback is not None:
        card_list = [fallback]
    updated = 0
    for card in card_list:
        try:
            if card["is_caption"]:
                await bot.edit_message_caption(
                    chat_id=card["chat_id"],
                    message_id=card["message_id"],
                    caption=card["base_html"] + status_line,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await bot.edit_message_text(
                    card["base_html"] + status_line,
                    chat_id=card["chat_id"],
                    message_id=card["message_id"],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
            updated += 1
        except Exception:  # noqa: BLE001 — карточка удалена, не изменилась и т.п.
            log.debug(
                "Карточка заявки %s в чате %s не обновлена",
                request_id, card["chat_id"], exc_info=True,
            )
    return updated
