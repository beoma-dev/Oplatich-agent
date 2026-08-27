"""Служебные пометки к заявке: причина смены статуса, отметка о напоминании.

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_nudges (
            request_id TEXT PRIMARY KEY,
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


def claim_nudge_sync(request_id: str, interval: float, now: float | None = None) -> float:
    """Занимает право напомнить по заявке. 0 — можно, иначе сколько ждать.

    Отметка живёт в БД, а не в памяти процесса: перезапуск бота не должен
    открывать окно для второго напоминания подряд, а перезапускается он
    при каждом деплое.

    Проверка и отметка — одним запросом в одной транзакции: два нажатия
    подряд (палец дрогнул, сеть переспросила) иначе оба увидели бы, что
    напоминаний ещё не было, и финансист получил бы дубль.
    """
    stamp = time.time() if now is None else now
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ts FROM request_nudges WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            left = float(row[0]) + interval - stamp
            if left > 0:
                return left
        conn.execute(
            "INSERT OR REPLACE INTO request_nudges (request_id, ts) VALUES (?, ?)",
            (request_id, stamp),
        )
    return 0.0


def forget_nudge_sync(request_id: str) -> None:
    """Снимает отметку — на случай, если разослать не удалось никому."""
    with _connect() as conn:
        conn.execute("DELETE FROM request_nudges WHERE request_id = ?", (request_id,))


async def claim_nudge(request_id: str, interval: float) -> float:
    return await asyncio.to_thread(claim_nudge_sync, request_id, interval)


async def forget_nudge(request_id: str) -> None:
    try:
        await asyncio.to_thread(forget_nudge_sync, request_id)
    except Exception:  # noqa: BLE001 — отметка вторична
        log.exception("Не удалось снять отметку о напоминании по %s", request_id)


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
