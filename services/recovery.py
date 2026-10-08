"""Жизненный цикл подтверждённых сбоев, переживающий перезапуск.

Успех фиксируется только тем сценарием, который проверил ту же операцию.
Уведомления доставляются каждому админу отдельно: сбой одной лички не
заставляет повторять сообщение остальным. Очередь повторяет пульс Telegram.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

from config import settings
from services import runtime_settings as rs

log = logging.getLogger(__name__)
_flush_lock = asyncio.Lock()


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_recovery (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, failure_title TEXT NOT NULL,
        started REAL NOT NULL, resolved REAL, title TEXT, details TEXT,
        delivered TEXT NOT NULL DEFAULT '[]', finished INTEGER NOT NULL DEFAULT 0
    )''')
    return conn


def fail_sync(key: str, kind: str, title: str, started: float | None = None) -> None:
    with _connect() as conn:
        conn.execute('''INSERT INTO alert_recovery(key, kind, failure_title, started)
            VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET
            kind=excluded.kind, failure_title=excluded.failure_title,
            started=CASE WHEN alert_recovery.resolved IS NULL THEN
                alert_recovery.started ELSE excluded.started END,
            resolved=NULL, title=NULL, details=NULL, delivered='[]', finished=0''',
            (key, kind, title, time.time() if started is None else started))


def get_sync(key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute('SELECT * FROM alert_recovery WHERE key=?', (key,)).fetchone()
        return dict(row) if row else None


def resolve_sync(key: str, title: str, details: str) -> dict | None:
    with _connect() as conn:
        changed = conn.execute('''UPDATE alert_recovery SET resolved=?, title=?, details=?
            WHERE key=? AND resolved IS NULL''', (time.time(), title, details, key))
    item = get_sync(key)
    if item:
        item["new_resolution"] = bool(changed.rowcount)
    return item


def remaining_sync(item: dict) -> bool:
    with _connect() as conn:
        return bool(conn.execute(
            'SELECT 1 FROM alert_recovery WHERE kind=? AND failure_title=? '
            'AND resolved IS NULL LIMIT 1',
            (item['kind'], item['failure_title']),
        ).fetchone())


def pending_sync() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM alert_recovery WHERE resolved IS NOT NULL AND finished=0')]


def acknowledge_sync(item: dict, delivered: list[int], finished: bool) -> None:
    with _connect() as conn:
        # Запоздалый успех старого уведомления не закрывает новый провал.
        conn.execute('''UPDATE alert_recovery SET delivered=?, finished=?
            WHERE key=? AND started=? AND resolved=?''',
            (json.dumps(delivered), int(finished), item['key'],
             item['started'], item['resolved']))


async def fail(key: str, kind: str, title: str) -> None:
    try:
        await asyncio.to_thread(fail_sync, key, kind, title)
    except Exception:  # noqa: BLE001 — датчик не ломает основной сценарий
        log.exception('Не удалось сохранить состояние сбоя')


async def recover(bot, key: str, title: str, details: str = '') -> None:
    try:
        item = await asyncio.to_thread(resolve_sync, key, title, details)
        if item and item['resolved'] is not None:
            if item.get("new_resolution") and not await asyncio.to_thread(remaining_sync, item):
                await asyncio.to_thread(rs.resolve_incident, item['kind'], item['failure_title'])
            await flush(bot)
    except Exception:  # noqa: BLE001 — подтверждение успеха не роняет операцию
        log.exception('Не удалось обработать восстановление')


async def flush(bot) -> None:
    async with _flush_lock:
        try:
            await _flush(bot)
        except Exception:  # noqa: BLE001 — пульс продолжает проверять связь
            log.exception("Не удалось отправить подтверждения восстановления")


async def _flush(bot) -> None:
    from services import alerts

    for item in await asyncio.to_thread(pending_sync):
        done = json.loads(item['delivered'])
        targets = rs.effective_admin_ids()
        if not rs.alert_kind_enabled(item['kind']):
            await asyncio.to_thread(acknowledge_sync, item, done, True)
            continue
        minutes = max(0, int((item['resolved'] - item['started']) / 60))
        details = item['details'] + f'\nОт первого сбоя до подтверждения: {minutes} мин.'
        for target in targets:
            if target in done:
                continue
            # Не применять троттлинг аварий к закрытию аварии. Не добавляем
            # восстановление в счётчик сбоев и не раскрываем сырые исключения.
            if await alerts._deliver(
                bot, alerts._format(item['title'], details, icon='✅'),
                [target], alerts._buttons(item['kind']),
            ):
                done.append(target)
                await asyncio.to_thread(acknowledge_sync, item, done, False)
        await asyncio.to_thread(
            acknowledge_sync, item, done, bool(targets) and all(t in done for t in targets)
        )
