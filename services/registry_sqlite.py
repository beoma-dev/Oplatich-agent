"""Первичное хранилище заявок: SQLite (локальный режим).

Источник правды для локального бэкенда: транзакционная запись, статусы,
выборки (подсказки контрагентов, будущие отчёты). Красивый xlsx остаётся
зеркалом/экспортом и ведётся фасадом storage поверх этого модуля.

Живёт в той же БД, что аудит и дедуп (SECURITY_DB_FILE) — один файл данных,
один бэкап. Синхронные функции — вызывать через to_thread из storage.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter

from bot.models import SHEET_HEADERS, InvoiceRequest
from config import settings

log = logging.getLogger(__name__)

# Порядок строго повторяет SHEET_HEADERS — маппинг колонок реестра.
_FIELDS = [
    "created_at",
    "planned_date",
    "sender",
    "counterparty",
    "amount",
    "article",
    "status",
    "comment",
    "file_link",
    "currency",
    "urgency",
    "requisites",
    "request_id",
    "telegram_id",
    "work_deadline",
]


def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            created_at TEXT, planned_date TEXT, sender TEXT,
            counterparty TEXT, amount TEXT, article TEXT, status TEXT,
            comment TEXT, file_link TEXT, currency TEXT, urgency TEXT,
            requisites TEXT, request_id TEXT UNIQUE NOT NULL, telegram_id TEXT,
            work_deadline TEXT
        )
        """
    )
    _add_missing_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_cp ON requests (counterparty)"
    )
    # Выборка «Мои заявки» — по автору.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_author ON requests (telegram_id)"
    )
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Досоздаёт колонки в УЖЕ существующей таблице.

    CREATE TABLE IF NOT EXISTS не трогает готовую таблицу, поэтому у бота,
    который уже работает с данными, новая колонка сама не появится — INSERT
    упал бы на «no such column». Идём по _FIELDS: чего нет, то добавляем.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    for field in _FIELDS:
        if field not in have:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {field} TEXT")
            log.info("Реестр SQLite: добавлена колонка %s", field)


def _row_to_sheet_dict(values: tuple) -> dict[str, str]:
    return dict(zip(SHEET_HEADERS, [str(v) if v is not None else "" for v in values], strict=True))


def append_sync(request: InvoiceRequest) -> int:
    """Пишет заявку. Возвращает порядковый номер записи."""
    values = request.as_sheet_row()  # порядок совпадает с _FIELDS
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO requests (ts, {', '.join(_FIELDS)}) "
            f"VALUES (?, {', '.join('?' * len(_FIELDS))})",
            (time.time(), *values),
        )
        (count,) = conn.execute("SELECT COUNT(*) FROM requests").fetchone()
    log.info("Заявка %s записана в SQLite (№%s)", request.request_id, count)
    return int(count)


def set_status_sync(request_id: str, status_text: str) -> dict[str, str] | None:
    """Меняет статус. Возвращает строку в формате SHEET_HEADERS или None."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status = ? WHERE request_id = ?",
            (status_text, request_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    log.info("Заявка %s: статус в SQLite → «%s»", request_id, status_text)
    return _row_to_sheet_dict(row)


def get_request_sync(request_id: str) -> dict[str, str] | None:
    """Заявка по ID в формате SHEET_HEADERS (None — такой заявки нет)."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return _row_to_sheet_dict(row) if row is not None else None


def recent_by_author_sync(telegram_id: int, limit: int) -> list[dict[str, str]]:
    """Последние заявки автора — новые сверху («Мои заявки»)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM requests "
            "WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (str(telegram_id), limit),
        ).fetchall()
    return [_row_to_sheet_dict(row) for row in rows]


def delete_sync(request_id: str) -> bool:
    """Удаляет заявку. True — такая заявка была."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM requests WHERE request_id = ?", (request_id,))
    if cur.rowcount:
        log.info("Заявка %s удалена из SQLite", request_id)
    return cur.rowcount > 0


def recent_requests_sync(limit: int) -> list[dict[str, str]]:
    """Последние заявки ВСЕХ авторов — панель финансиста (новые сверху)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_FIELDS)} FROM requests ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_sheet_dict(row) for row in rows]


def counterparty_book_sync(limit: int) -> list[dict[str, str]]:
    """Справочник контрагентов: имя + последние известные реквизиты.

    Порядок — как у подсказок: сначала частые, при равенстве — свежие.
    Реквизиты берём из самой поздней заявки, где они непустые: контрагент
    мог однажды прийти со счётом (реквизиты пустые), а однажды без.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT counterparty, requisites, id FROM requests ORDER BY id"
        ).fetchall()
    counter: Counter[str] = Counter()
    last_pos: dict[str, int] = {}
    requisites: dict[str, str] = {}
    for name, req, pos in rows:
        name = (name or "").strip()
        if not name:
            continue
        counter[name] += 1
        last_pos[name] = pos
        if (req or "").strip():
            requisites[name] = req.strip()
    ordered = sorted(counter, key=lambda n: (-counter[n], -last_pos[n]))
    return [
        {"name": name, "requisites": requisites.get(name, "")}
        for name in ordered[:limit]
    ]


def recent_counterparties_sync(limit: int) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT counterparty, id FROM requests ORDER BY id"
        ).fetchall()
    counter: Counter[str] = Counter()
    last_pos: dict[str, int] = {}
    for name, pos in rows:
        name = (name or "").strip()
        if name:
            counter[name] += 1
            last_pos[name] = pos
    ordered = sorted(counter, key=lambda n: (-counter[n], -last_pos[n]))
    return ordered[:limit]


def import_row_sync(values: list[str]) -> bool:
    """Импорт строки из старого xlsx (значения в порядке SHEET_HEADERS).

    INSERT OR IGNORE по request_id: повторный запуск миграции безопасен.
    Возвращает True, если строка добавлена.
    """
    padded = list(values) + [""] * (len(_FIELDS) - len(values))
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO requests (ts, {', '.join(_FIELDS)}) "
            f"VALUES (?, {', '.join('?' * len(_FIELDS))})",
            (time.time(), *padded[: len(_FIELDS)]),
        )
        return cur.rowcount == 1


def has_request_sync(request_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
    return row is not None


def all_request_ids_sync() -> list[str]:
    """Все ID заявок в реестре — для сверки с xlsx-зеркалом."""
    with _connect() as conn:
        return [
            str(row[0])
            for row in conn.execute("SELECT request_id FROM requests ORDER BY id")
            if row[0]
        ]
