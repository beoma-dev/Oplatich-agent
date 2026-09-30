"""Карточки заявок на доступ: где лежит каждая разосланная.

Просьбу о доступе видят ВСЕ админы, а решает её один. Без этой таблицы
карточки остальных остаются с живыми кнопками, и второй админ жмёт по уже
рассмотренной заявке. Безобидно это не выглядит только на первый взгляд:
30.09.2026 прогон показал, что «Отказать» после чужого «Открыть доступ»
оставляет доступ ОТКРЫТЫМ, пишет человеку «в доступе отказано» и кладёт в
аудит отказ. Три картины мира расходятся разом.

Ровно тот же урок уже записан в services/cards.py про карточки заявок:
обновлять надо у всех, а не только у нажавшего. Сюда он не доехал.

Та же SQLite, что аудит/дедуп/карточки. Синхронно — через to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from config import settings

log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_cards (
            target_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            base_html TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_access_cards_target ON access_cards (target_id)"
    )
    return conn


def save_sync(target_id: int, chat_id: int, message_id: int, base_html: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO access_cards "
            "(target_id, chat_id, message_id, base_html) VALUES (?, ?, ?, ?)",
            (target_id, chat_id, message_id, base_html),
        )


def for_target_sync(target_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, message_id, base_html FROM access_cards WHERE target_id = ?",
            (target_id,),
        ).fetchall()
    return [
        {"chat_id": chat_id, "message_id": message_id, "base_html": base_html}
        for chat_id, message_id, base_html in rows
    ]


def clear_target_sync(target_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM access_cards WHERE target_id = ?", (target_id,))


async def save(target_id: int, chat_id: int, message_id: int, base_html: str) -> None:
    """Запоминает карточку; сбой не срывает рассылку."""
    try:
        await asyncio.to_thread(save_sync, target_id, chat_id, message_id, base_html)
    except Exception:  # noqa: BLE001 — заявка важнее записи о карточке
        log.exception("Не удалось запомнить карточку заявки на доступ %s", target_id)


async def for_target(target_id: int) -> list[dict]:
    try:
        return await asyncio.to_thread(for_target_sync, target_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать карточки заявки на доступ %s", target_id)
        return []


async def clear_target(target_id: int) -> None:
    try:
        await asyncio.to_thread(clear_target_sync, target_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось убрать карточки заявки на доступ %s", target_id)
