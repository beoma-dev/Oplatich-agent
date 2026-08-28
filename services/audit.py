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

from bot.models import REQUEST_STATUSES
from config import settings

log = logging.getLogger(__name__)

# События (детали — свободный текст в details).
ACCESS_DENIED = "ACCESS_DENIED"          # не в whitelist: чат-форма или API
ADMIN_DENIED = "ADMIN_DENIED"            # попытка админ-действия без прав
STATUS_DENIED = "STATUS_DENIED"          # смена статуса не финансистом
RATE_LIMITED = "RATE_LIMITED"            # превышен лимит подачи
GROUP_POST_REJECTED = "GROUP_POST_REJECTED"  # подделанный return_chat
REQUEST_SUBMITTED = "REQUEST_SUBMITTED"  # заявка записана в реестр
CLOSING_DOCS = "CLOSING_DOCS"            # к оплаченной заявке приложили акт/УПД
OVERDUE_NUDGE = "OVERDUE_NUDGE"          # автор напомнил о просрочке
REQUEST_FAILED = "REQUEST_FAILED"        # заявка не сохранилась
STATUS_CHANGED = "STATUS_CHANGED"        # финансист сменил статус
DUPLICATE_CONFIRMED = "DUPLICATE_CONFIRMED"  # дубль отправлен осознанно
REGISTRY_EXPORTED = "REGISTRY_EXPORTED"      # админ выгрузил xlsx-реестр
BACKUP_SETTINGS = "BACKUP_SETTINGS"          # админ изменил настройки бэкапа
REMINDER_SETTINGS = "REMINDER_SETTINGS"      # админ изменил напоминания
BETA_SETTINGS = "BETA_SETTINGS"              # админ включил/выключил бету
ALERT_SETTINGS = "ALERT_SETTINGS"            # админ изменил уведомления о сбоях
MAINTENANCE = "MAINTENANCE"                  # админ повесил или снял плашку работ
ADMIN_ROLE = "ADMIN_ROLE"                    # админ назначен или разжалован
ACCESS_REQUESTED = "ACCESS_REQUESTED"        # сотрудник попросил доступ
ACCESS_RESOLVED = "ACCESS_RESOLVED"          # админ решил по заявке на доступ
FILE_SUSPICIOUS = "FILE_SUSPICIOUS"          # вложение не похоже на счёт
REQUEST_WITHDRAWN = "REQUEST_WITHDRAWN"      # автор отозвал свою заявку
WITHDRAW_DENIED = "WITHDRAW_DENIED"          # попытка отозвать чужую/непустую заявку
FINANCE_DENIED = "FINANCE_DENIED"            # панель финансиста без прав
REQUEST_DELETED = "REQUEST_DELETED"          # заявка удалена из реестра
RESTORE_APPLIED = "RESTORE_APPLIED"          # данные восстановлены из архива
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


def paid_times_sync() -> dict[str, float]:
    """{ID заявки: когда её отметили оплаченной} — для «подача → оплата».

    Единственное место, где это время вообще есть: в реестре хранится статус,
    но не момент его смены. Берём ПЕРВУЮ отметку по каждой заявке: статус
    можно переставить и обратно, а нас интересует, когда деньги ушли.

    Формат details задаётся services.status_change: «INV-… → Оплачена» плюс
    необязательное « · причина: …». Разбор держится на этой строке — меняете
    её, поправьте и здесь (стережёт tests/test_analytics.py).
    """
    paid = REQUEST_STATUSES["PAID"][1]
    out: dict[str, float] = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, details FROM audit WHERE event = ? ORDER BY id",
            (STATUS_CHANGED,),
        ).fetchall()
    for ts, details in rows:
        head = str(details or "").split(" · ")[0]
        if " → " not in head:
            continue
        request_id, _, status = head.partition(" → ")
        if status.strip() != paid:
            continue
        out.setdefault(request_id.strip(), float(ts))
    return out


async def paid_times() -> dict[str, float]:
    return await asyncio.to_thread(paid_times_sync)


def submitters_sync() -> set[int]:
    """Кто хоть раз ПОДАВАЛ заявку.

    Только настоящие подачи: закрывающие документы и напоминания о просрочке
    когда-то писались тем же событием REQUEST_SUBMITTED (теперь у них свои),
    и в старых записях они неотличимы иначе как по форме details — у подачи
    там «INV-… · сумма · контрагент».
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM audit "
            "WHERE event = ? AND details LIKE '% · %' AND user_id IS NOT NULL",
            (REQUEST_SUBMITTED,),
        ).fetchall()
    return {int(r[0]) for r in rows}


async def submitters() -> set[int]:
    return await asyncio.to_thread(submitters_sync)


async def recent_events(limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(recent_events_sync, limit)
