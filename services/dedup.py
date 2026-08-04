"""Дедуп заявок: защита от двойной подачи одного и того же счёта.

Отпечаток — SHA-256 от нормализованных ключевых полей (контрагент + сумма +
валюта + статья + плановая дата): регистр и лишние пробелы не влияют,
поэтому «ООО Ромашка» и «ооо  ромашка» дают один хэш. Отпечатки хранятся
в той же SQLite, что и аудит. Если такой же отпечаток встречался за
последние DEDUP_WINDOW_DAYS — пользователю показывается предупреждение
«похоже, такая заявка уже подавалась», и отправка требует подтверждения.

Ловит типовой сценарий двойной оплаты: сотрудник подал заявку дважды
(не увидел подтверждения) или два человека подали один и тот же счёт.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.models import InvoiceRequest
from config import settings

log = logging.getLogger(__name__)


def fingerprint(request: InvoiceRequest) -> str:
    """SHA-256 от нормализованных ключевых полей заявки."""
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", str(value)).strip().lower()

    planned = request.planned_date.isoformat() if request.planned_date else ""
    key = "|".join(
        [
            norm(request.counterparty),
            f"{request.amount:.2f}",
            norm(request.currency),
            norm(request.article),
            planned,
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dedup (
            fingerprint TEXT NOT NULL,
            ts REAL NOT NULL,
            request_id TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_fp ON dedup (fingerprint, ts)")
    return conn


def check_sync(fp: str) -> str | None:
    """Дата последней похожей заявки в окне дедупа (или None)."""
    if settings.dedup_window_days <= 0:
        return None
    cutoff = time.time() - settings.dedup_window_days * 86400
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM dedup WHERE fingerprint = ? AND ts > ?",
            (fp, cutoff),
        ).fetchone()
    if not row or row[0] is None:
        return None
    tz = ZoneInfo(settings.timezone)
    return datetime.fromtimestamp(row[0], tz).strftime("%d.%m.%Y %H:%M")


def remember_sync(fp: str, request_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO dedup (fingerprint, ts, request_id) VALUES (?, ?, ?)",
            (fp, time.time(), request_id),
        )


async def check_duplicate(request: InvoiceRequest) -> str | None:
    """Асинхронно: дата похожей заявки в окне или None. Ошибки не всплывают."""
    try:
        return await asyncio.to_thread(check_sync, fingerprint(request))
    except Exception:  # noqa: BLE001 — дедуп вторичен
        log.exception("Сбой проверки дублей")
        return None


async def remember(request: InvoiceRequest) -> None:
    """Асинхронно запоминает отпечаток заявки. Ошибки не всплывают."""
    try:
        await asyncio.to_thread(remember_sync, fingerprint(request), request.request_id)
    except Exception:  # noqa: BLE001
        log.exception("Сбой записи отпечатка дедупа")
