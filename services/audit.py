"""Аудит-журнал: «кто что делал» — для службы безопасности.

События пишутся в SQLite (таблица audit) с меткой времени, user_id и
username: отказы доступа, поданные заявки, сбои, смены статусов. В отличие
от stdout-логов, журнал структурирован, переживает ротацию и отвечает на
вопрос «кто пытался и кому отказали» одним запросом (или командой /audit).

Аудит вторичен по отношению к сценарию: любая ошибка записи логируется
и не влияет на работу бота.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings

log = logging.getLogger(__name__)

# События (детали — свободный текст в details).
ACCESS_DENIED = "ACCESS_DENIED"          # не в whitelist: чат-форма или API
ADMIN_DENIED = "ADMIN_DENIED"            # попытка админ-действия без прав
STATUS_DENIED = "STATUS_DENIED"          # смена статуса не финансистом
RATE_LIMITED = "RATE_LIMITED"            # превышен лимит подачи
GROUP_POST_REJECTED = "GROUP_POST_REJECTED"  # подделанный return_chat
REQUEST_SUBMITTED = "REQUEST_SUBMITTED"  # заявка записана в реестр
REQUEST_FAILED = "REQUEST_FAILED"        # заявка не сохранилась
STATUS_CHANGED = "STATUS_CHANGED"        # финансист сменил статус
DUPLICATE_CONFIRMED = "DUPLICATE_CONFIRMED"  # дубль отправлен осознанно
REGISTRY_EXPORTED = "REGISTRY_EXPORTED"      # админ выгрузил xlsx-реестр
BACKUP_SETTINGS = "BACKUP_SETTINGS"          # админ изменил настройки бэкапа
REMINDER_SETTINGS = "REMINDER_SETTINGS"      # админ изменил напоминания
BETA_SETTINGS = "BETA_SETTINGS"              # админ включил/выключил бету
ALERT_SETTINGS = "ALERT_SETTINGS"            # админ изменил уведомления о сбоях
ADMIN_ROLE = "ADMIN_ROLE"                    # админ назначен или разжалован
ACCESS_REQUESTED = "ACCESS_REQUESTED"        # сотрудник попросил доступ
ACCESS_RESOLVED = "ACCESS_RESOLVED"          # админ решил по заявке на доступ
FILE_SUSPICIOUS = "FILE_SUSPICIOUS"          # вложение не похоже на счёт
REQUEST_WITHDRAWN = "REQUEST_WITHDRAWN"      # автор отозвал свою заявку
WITHDRAW_DENIED = "WITHDRAW_DENIED"          # попытка отозвать чужую/непустую заявку
FINANCE_DENIED = "FINANCE_DENIED"            # панель финансиста без прав
REQUEST_DELETED = "REQUEST_DELETED"          # заявка удалена из реестра
DELETE_DENIED = "DELETE_DENIED"              # попытка удалить без прав


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            event TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            details TEXT
        )
        """
    )
    return conn


def log_event_sync(
    event: str,
    user_id: int | None = None,
    username: str | None = None,
    details: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit (ts, event, user_id, username, details) VALUES (?, ?, ?, ?, ?)",
            (time.time(), event, user_id, username, details[:500]),
        )


async def log_event(
    event: str,
    user_id: int | None = None,
    username: str | None = None,
    details: str = "",
) -> None:
    """Пишет событие; никогда не бросает исключений."""
    try:
        await asyncio.to_thread(log_event_sync, event, user_id, username, details)
    except Exception:  # noqa: BLE001 — аудит не должен ломать сценарий
        log.exception("Не удалось записать событие аудита %s", event)


def recent_events_sync(limit: int = 15) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, event, user_id, username, details FROM audit "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    tz = ZoneInfo(settings.timezone)
    return [
        {
            "ts": datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M:%S"),
            "event": event,
            "user_id": user_id,
            "username": username,
            "details": details,
        }
        for ts, event, user_id, username, details in rows
    ]


async def recent_events(limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(recent_events_sync, limit)
