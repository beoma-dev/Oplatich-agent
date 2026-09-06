"""Смена канала до Telegram, когда текущий замолчал.

Живой подмены клиента внутри PTB нет и не задумано: канал вшивается в Bot при
сборке (`proxy.build_requests`), а под ним висит начатый long polling. Зато
`resolve_proxy()` перебирает кандидатов при КАЖДОМ старте, а контейнер поднимает
себя сам (`restart: unless-stopped`). Значит, «переключиться на запасной» — это
аккуратно выйти: через несколько секунд процесс вернётся уже по живому каналу.
Замер на боевом 06.09.2026: от «Запуск бота» до «Бот запущен» — 4 секунды.

Отметка о последнем таком выходе живёт в SQLite, а НЕ в памяти процесса: память
как раз и обнуляется перезапуском, и кулдаун без диска не работал бы вовсе —
бот уходил бы в круг из перезапусков ровно тогда, когда лежит не прокси,
а Telegram или вся сеть.

Синхронные функции — вызывать через to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import time

from config import settings
from services import runtime_settings as rs

log = logging.getLogger(__name__)

# Сколько канал должен молчать, прежде чем менять его. Пульс бьётся раз в
# минуту и сам повторяет попытки, так что три минуты — это три подряд
# провалившихся такта, а не одна потерянная посылка. WARP переподключает
# туннель десятки раз в сутки, и каждое переподключение длится секунды:
# порог обязан быть заметно больше такого окна.
SWITCH_AFTER = 180.0

# Не чаще раза в час. Если лёг не прокси, а Telegram или сеть целиком,
# перезапуск не поможет ничем — а крутиться в цикле он не должен.
COOLDOWN = 3600.0

_MARK = "channel_switch"


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS process_marks (
            key TEXT PRIMARY KEY,
            ts  REAL NOT NULL
        )
        """
    )
    return conn


def last_switch_sync() -> float:
    """Когда в последний раз уходили на запасной канал. 0.0 — никогда."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT ts FROM process_marks WHERE key = ?", (_MARK,)
            ).fetchone()
        return float(row[0]) if row else 0.0
    except sqlite3.Error:
        # Читать отметку не вышло — считаем, что переключений не было. Ошибиться
        # в эту сторону безопаснее: худшее, что будет, — лишний перезапуск.
        log.exception("Не удалось прочитать отметку о смене канала")
        return 0.0


def mark_switch_sync(when: float | None = None) -> None:
    """Ставит отметку ДО выхода: после него писать будет некому."""
    stamp = time.time() if when is None else when
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO process_marks (key, ts) VALUES (?, ?)",
                (_MARK, stamp),
            )
    except sqlite3.Error:
        log.exception("Не удалось записать отметку о смене канала")


def should_switch(down_seconds: float, now: float | None = None) -> tuple[bool, str]:
    """Пора ли уходить на запасной канал. Вторым — причина отказа, для лога."""
    if len(settings.proxy_urls) < 2:
        return False, "запасного канала нет — в PROXY_URL один адрес"
    if down_seconds < SWITCH_AFTER:
        return False, f"молчит {down_seconds:.0f} с, порог {SWITCH_AFTER:.0f} с"
    stamp = time.time() if now is None else now
    since = stamp - last_switch_sync()
    if since < COOLDOWN:
        return False, f"уже переключались {since / 60:.0f} мин назад, ждём часа"
    return True, ""


def request_restart() -> None:
    """Просит процесс завершиться. Вынесено отдельно — чтобы тесты не умирали.

    SIGTERM, а не os._exit: uvicorn ставит на него обработчик, serve()
    возвращается, и main аккуратно гасит бота (см. _run_bot_with_api).
    """
    os.kill(os.getpid(), signal.SIGTERM)


async def maybe_switch(down_seconds: float) -> bool:
    """Проверяет условия и, если пора, уводит процесс на перезапуск.

    True — решение принято, процесс уже завершается. Уведомить админа отсюда
    НЕЛЬЗЯ: канал как раз и молчит, сообщение просто не уйдёт. Поэтому пишем
    в журнал инцидентов (он локальный) и в лог, а рассказать о случившемся
    успеет пульс — сообщением «Связь восстановлена» уже по новому каналу.
    """
    ok, why = should_switch(down_seconds)
    if not ok:
        log.debug("Смена канала не нужна: %s", why)
        return False

    minutes = max(1, int(round(down_seconds / 60)))
    details = (
        f"Канал молчит {minutes} мин. Перезапускаюсь: при старте бот заново "
        f"переберёт адреса из PROXY_URL и уйдёт на первый отвечающий."
    )
    log.error("Смена канала до Telegram: %s", details)
    await asyncio.to_thread(mark_switch_sync)
    try:
        rs.record_incident(
            "telegram",
            "Меняю канал до Telegram",
            sent=False,
            when=time.time(),
            details=details,
        )
    except Exception:  # noqa: BLE001 — журнал не должен мешать перезапуску
        log.exception("Не удалось записать инцидент о смене канала")
    request_restart()
    return True
