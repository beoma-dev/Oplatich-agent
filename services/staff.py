"""Справочник сотрудников: Telegram-аккаунт → подтверждённое ФИО.

Зачем. В реестр попадает не имя из Telegram-профиля (его человек меняет
когда захочет и пишет там что угодно), а ФИО, согласованное в компании.
Раньше соответствие держалось в `.env` (EMPLOYEE_NAMES): править мог только
тот, у кого есть доступ к серверу, и бот приходилось перезапускать.

Справочник живёт В НАШЕЙ БАЗЕ и является источником правды. Google-таблица —
только способ занести список первый раз или влить обновления пачкой; бот
в неё не ходит ни при подаче заявки, ни по расписанию. Так у справочника
нет ни внешней зависимости, ни чужих прав доступа, а бэкап забирает его
вместе с остальными данными — он в том же файле SQLite.

Импорт СЛИВАЕТ, а не заменяет: в базе есть люди, добавленные руками, и
затирать их содержимым таблицы нельзя. Кого в таблице нет — показываем
админу списком, но не удаляем: увольнение — решение человека, а не разница
двух списков.

Разбор таблицы (`parse`) — ЧИСТАЯ функция, и он терпимый: заголовков может
не быть вовсе (в исходной таблице их нет), колонки могут стоять в любом
порядке. Что не разобралось — попадает в замечания и видно админу, потому
что молча потерянная строка хуже явной жалобы.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time

from config import settings

log = logging.getLogger(__name__)

_NAME_HEADERS = ("фио", "сотрудник", "имя", "full name", "name")
_USER_HEADERS = ("telegram", "телеграм", "аккаунт", "username", "юзернейм", "ник", "логин")
_ID_HEADERS = ("id", "ид")
_ROLE_HEADERS = ("должность", "роль", "position")

# Telegram: 5–32 символа, буквы/цифры/подчёркивание. Берём и «@petya»,
# и «t.me/petya», и «https://t.me/petya?start=1» — в таблицах живёт всё это.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_username(value: object) -> str:
    """Любая запись аккаунта → «petya». Пусто, если это не аккаунт."""
    raw = _norm(value)
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].rstrip("/")
    raw = raw.rsplit("/", 1)[-1].lstrip("@")
    return raw.lower() if _USERNAME_RE.match(raw) else ""


def _pick(titles: list[str], wanted: tuple[str, ...]) -> int | None:
    for index, title in enumerate(titles):
        low = title.lower()
        if any(word in low for word in wanted):
            return index
    return None


def _looks_like_name(value: str) -> bool:
    """ФИО — хотя бы одна буква и не похоже на аккаунт или число."""
    return any(ch.isalpha() for ch in value) and not normalize_username(value)


def parse(values: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Строки таблицы → (записи, замечания)."""
    notes: list[str] = []
    head_at: int | None = None
    for index, row in enumerate(values[:5]):
        titles = [_norm(c) for c in row]
        if _pick(titles, _NAME_HEADERS) is not None and (
            _pick(titles, _USER_HEADERS) is not None
            or _pick(titles, _ID_HEADERS) is not None
        ):
            head_at = index
            break

    col_name = col_user = col_id = col_role = None
    if head_at is not None:
        titles = [_norm(c) for c in values[head_at]]
        col_name = _pick(titles, _NAME_HEADERS)
        col_user = _pick(titles, _USER_HEADERS)
        col_id = _pick(titles, _ID_HEADERS)
        col_role = _pick(titles, _ROLE_HEADERS)
    body = values[head_at + 1:] if head_at is not None else values
    first_line = (head_at + 2) if head_at is not None else 1

    def cell(row: list[str], index: int | None) -> str:
        return _norm(row[index]) if index is not None and index < len(row) else ""

    seen_users: set[str] = set()
    seen_ids: set[int] = set()
    out: list[dict] = []
    for offset, row in enumerate(body, start=first_line):
        cells = [_norm(c) for c in row]
        if not any(cells):
            continue
        if head_at is not None:
            full_name, raw_user, raw_id = (
                cell(row, col_name), cell(row, col_user), cell(row, col_id)
            )
            role = cell(row, col_role)
        else:
            # Без заголовков: аккаунт узнаём по виду, ФИО — первая ячейка,
            # где есть буквы и это не аккаунт. Порядок колонок неважен.
            raw_user = next((c for c in cells if normalize_username(c)), "")
            full_name = next((c for c in cells if c != raw_user and _looks_like_name(c)), "")
            raw_id = next((c for c in cells if c.isdigit() and len(c) >= 6), "")
            role = ""

        username = normalize_username(raw_user)
        if raw_user and not username:
            notes.append(f"строка {offset}: не разобрал аккаунт «{raw_user}»")
        tg_id = int(raw_id) if raw_id.isdigit() else None
        if raw_id and tg_id is None:
            notes.append(f"строка {offset}: id «{raw_id}» — не число")
        if not full_name:
            notes.append(f"строка {offset}: не нашёл ФИО")
            continue
        if not username and tg_id is None:
            notes.append(f"строка {offset}: «{full_name}» без аккаунта и без id")
            continue
        # Дубли: чей аккаунт вписан дважды — какое ФИО верное, решать не нам.
        if username and username in seen_users:
            notes.append(f"строка {offset}: @{username} уже встречался выше")
            continue
        if tg_id is not None and tg_id in seen_ids:
            notes.append(f"строка {offset}: id {tg_id} уже встречался выше")
            continue
        if username:
            seen_users.add(username)
        if tg_id is not None:
            seen_ids.add(tg_id)
        out.append({"tg_id": tg_id, "username": username,
                    "full_name": full_name, "role": role})
    return out, notes


# ---------------------------------------------------------------------------
# Хранение: та же SQLite, что аудит и реестр, — один файл, один бэкап.
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    path = settings.security_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT,
            added REAL NOT NULL
        )
        """
    )
    return conn


def all_sync() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tg_id, username, full_name, role FROM staff "
            "ORDER BY full_name COLLATE NOCASE"
        ).fetchall()
    return [
        {"id": i, "tg_id": tg, "username": user, "full_name": name, "role": role or ""}
        for i, tg, user, name, role in rows
    ]


def add_sync(full_name: str, username: str = "", tg_id: int | None = None,
             role: str = "") -> str:
    """Добавляет или обновляет запись. Возвращает «added» либо «updated».

    Ключ — аккаунт: тот же @username с другим ФИО означает, что человека
    переименовали (замужество, опечатку поправили), а не что их двое.
    """
    name = _norm(full_name)
    handle = normalize_username(username)
    if not name:
        raise ValueError("Без ФИО запись бессмысленна.")
    if tg_id is not None and tg_id <= 0:
        tg_id = None
    if not handle and tg_id is None:
        raise ValueError("Нужен @username или числовой id — иначе некого узнавать.")
    stamp = time.time()
    with _connect() as conn:
        found = None
        if tg_id is not None:
            found = conn.execute("SELECT id FROM staff WHERE tg_id = ?", (tg_id,)).fetchone()
        if found is None and handle:
            found = conn.execute(
                "SELECT id FROM staff WHERE username = ?", (handle,)
            ).fetchone()
        if found is not None:
            conn.execute(
                "UPDATE staff SET full_name = ?, role = ?, "
                "username = COALESCE(?, username), tg_id = COALESCE(?, tg_id) WHERE id = ?",
                (name, _norm(role) or None, handle or None, tg_id, found[0]),
            )
            return "updated"
        conn.execute(
            "INSERT INTO staff (tg_id, username, full_name, role, added) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_id, handle or None, name, _norm(role) or None, stamp),
        )
    return "added"


def remove_sync(row_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM staff WHERE id = ?", (row_id,))
    return cur.rowcount > 0


def full_name_sync(user_id: int, username: str | None) -> str | None:
    """ФИО по аккаунту. Сначала по id — его человек сменить не может;
    по @username ищем, только если id в записи нет.

    Ноль и отрицательные id не ищем вовсе: настоящих таких не бывает, а
    попади такой в таблицу — он совпал бы с любым «неизвестным» и выдал бы
    чужое ФИО (поймано на живой проверке).
    """
    handle = normalize_username(username or "")
    with _connect() as conn:
        row = None
        if user_id > 0:
            row = conn.execute(
                "SELECT full_name FROM staff WHERE tg_id = ?", (user_id,)
            ).fetchone()
        if row is None and handle:
            row = conn.execute(
                "SELECT full_name FROM staff WHERE username = ?", (handle,)
            ).fetchone()
    return row[0] if row else None


def learn_id_sync(user_id: int, username: str | None) -> None:
    """Проставляет числовой id записи, найденной по @username.

    Аккаунт человек может сменить, id — нет. Первый раз узнаём id, когда
    он подаёт заявку, и дальше запись держится уже на нём.
    """
    handle = normalize_username(username or "")
    if not handle or user_id <= 0:
        return
    with _connect() as conn:
        try:
            conn.execute(
                "UPDATE staff SET tg_id = ? WHERE username = ? AND tg_id IS NULL",
                (user_id, handle),
            )
        except sqlite3.IntegrityError:
            # Этот id уже стоит у другой записи: два аккаунта на одного
            # человека или опечатка в справочнике. Разбирать это молча
            # нельзя, а ломать подачу заявки — тем более: оставляем как есть.
            log.warning("Справочник: id %s уже занят, @%s не связан", user_id, handle)


def resolve(user_id: int, username: str | None, fallback: str) -> str:
    """ФИО для реестра: справочник → запись из .env → имя из Telegram.

    Никогда не пусто и никогда не мешает подаче: справочник вторичен, и
    сорвать им приём заявки хуже, чем записать имя из профиля.
    """
    try:
        found = full_name_sync(user_id, username)
        if found:
            learn_id_sync(user_id, username)
    except Exception:  # noqa: BLE001 — справочник вторичен
        log.exception("Справочник сотрудников недоступен")
        found = None
    return found or settings.employee_name_for(user_id) or fallback


def import_rows(records: list[dict]) -> dict:
    """Вливает записи. Ничего не удаляет — только добавляет и обновляет."""
    added = updated = 0
    for item in records:
        try:
            outcome = add_sync(
                item["full_name"], item.get("username", ""), item.get("tg_id"),
                item.get("role", ""),
            )
        except ValueError:
            continue
        added += outcome == "added"
        updated += outcome == "updated"
    known = {r["username"] for r in records if r["username"]}
    extra = [
        r["full_name"] for r in all_sync()
        if r["username"] and r["username"] not in known
    ]
    return {"added": added, "updated": updated, "not_in_sheet": sorted(extra)}


def fetch_sync() -> list[list[str]]:
    """Читает таблицу-источник. Только по кнопке админа — при подаче заявки
    бот в Google не ходит."""
    from services.google_backend import _sheets

    out = (
        _sheets()
        .spreadsheets()
        .values()
        .get(
            spreadsheetId=settings.staff_sheet_id.strip(),
            range=settings.staff_sheet_range.strip() or "A1:Z500",
        )
        .execute()
    )
    return out.get("values", [])


async def import_from_sheet() -> dict:
    if not settings.staff_sheet_id.strip():
        return {"error": "Таблица-источник не задана (STAFF_SHEET_ID)."}
    try:
        values = await asyncio.to_thread(fetch_sync)
    except Exception as exc:  # noqa: BLE001 — покажем админу человеческим текстом
        log.exception("Справочник: не удалось прочитать таблицу")
        return {"error": _explain(exc)}
    records, notes = parse(values)
    if not records:
        return {"error": "; ".join(notes[:3]) or "В таблице нечего импортировать."}
    result = await asyncio.to_thread(import_rows, records)
    result["notes"] = notes[:10]
    return result


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "403" in text or "permission" in text.lower():
        return ("Нет доступа к таблице. Дайте сервисному аккаунту бота права "
                "«Читатель» на этот документ.")
    if "404" in text:
        return "Таблица не найдена — проверьте STAFF_SHEET_ID."
    return "Не удалось прочитать таблицу справочника."


async def all_staff() -> list[dict]:
    return await asyncio.to_thread(all_sync)


async def add(full_name: str, username: str = "", tg_id: int | None = None) -> str:
    return await asyncio.to_thread(add_sync, full_name, username, tg_id, "")


async def remove(row_id: int) -> bool:
    return await asyncio.to_thread(remove_sync, row_id)
