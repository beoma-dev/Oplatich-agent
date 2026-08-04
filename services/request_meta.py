"""Служебные пометки к заявке: причина смены статуса.

Причина живёт не в реестре (порядок колонок реестра — по ТЗ и менять его
нельзя), а рядом, в той же SQLite, что аудит/дедуп/карточки. Нужна, чтобы
автор видел в «Моих заявках», почему заявку отклонили или отложили, даже
если пуш-уведомление в момент смены статуса не дошло.

Синхронные функции — вызывать через to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from config import settings

log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_reasons (
            request_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    return conn


def save_reason_sync(request_id: str, status: str, reason: str, actor: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_reasons "
            "(request_id, status, reason, actor, ts) VALUES (?, ?, ?, ?, ?)",
            (request_id, status, reason, actor, time.time()),
        )


def reasons_for_sync(request_ids: list[str]) -> dict[str, str]:
    """Причины по списку заявок: {request_id: причина}."""
    if not request_ids:
        return {}
    placeholders = ", ".join("?" * len(request_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT request_id, reason FROM request_reasons "
            f"WHERE request_id IN ({placeholders})",
            request_ids,
        ).fetchall()
    return dict(rows)


async def save_reason(request_id: str, status: str, reason: str, actor: str) -> None:
    """Запоминает причину; сбой не срывает смену статуса."""
    try:
        await asyncio.to_thread(save_reason_sync, request_id, status, reason, actor)
    except Exception:  # noqa: BLE001 — пометка вторична
        log.exception("Не удалось сохранить причину по заявке %s", request_id)


async def reasons_for(request_ids: list[str]) -> dict[str, str]:
    try:
        return await asyncio.to_thread(reasons_for_sync, request_ids)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать причины смены статуса")
        return {}
