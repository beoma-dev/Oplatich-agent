#!/usr/bin/env python3
"""Preflight: проверка конфигурации ДО запуска бота.

Ошибки доступов (токен, права на таблицу/папку, каталоги) всплывают сразу
и по-русски, а не на первой заявке сотрудника.

Запуск:  python scripts/preflight.py        (локально, из корня проекта)
         docker compose run --rm app python scripts/preflight.py
Код возврата: 0 — всё готово, 1 — есть проблемы.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

OK, FAIL = "✅", "❌"
failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        print(f"{OK} {name}{f' — {detail}' if detail else ''}")
    except Exception as exc:  # noqa: BLE001 — печатаем любую причину
        failures.append(name)
        print(f"{FAIL} {name} — {exc}")


def check_telegram() -> str:
    import httpx

    proxy = settings.proxy_url or None
    with httpx.Client(proxy=proxy, timeout=15) as client:
        resp = client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
        )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getMe: {data.get('description', resp.status_code)}")
    return f"бот @{data['result']['username']}" + (" (через прокси)" if proxy else "")


def check_local_storage() -> str:
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    probe = settings.storage_path / ".preflight"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return str(settings.storage_path)


def check_security_db() -> str:
    from services import audit

    audit.recent_events_sync(1)
    return str(settings.security_db_path)


def check_google_sheets() -> str:
    from services.google_backend import _sheets

    meta = (
        _sheets()
        .spreadsheets()
        .get(spreadsheetId=settings.google_sheet_id, fields="properties.title")
        .execute()
    )
    return f"таблица «{meta['properties']['title']}»"


def check_google_drive() -> str:
    from services.google_backend import _drive

    folder = (
        _drive()
        .files()
        .get(
            fileId=settings.google_drive_folder_id,
            fields="name",
            supportsAllDrives=True,
        )
        .execute()
    )
    return f"папка «{folder['name']}»"


def main() -> int:
    print(f"Preflight · бэкенд: {settings.storage_backend} · TZ: {settings.timezone}\n")

    check("Telegram Bot API (getMe)", check_telegram)
    check("Локальное хранилище / данные", check_local_storage)
    check("SQLite аудита/дедупа", check_security_db)

    if settings.storage_is_google:
        if not settings.google_credentials_path.exists():
            failures.append("Google credentials")
            print(f"{FAIL} Google credentials — нет файла {settings.google_credentials_path}")
        else:
            check("Google Sheets (доступ к таблице)", check_google_sheets)
            check("Google Drive (доступ к папке)", check_google_drive)

    if not settings.admin_ids:
        print("⚠️  ADMIN_IDS пуст — админ-панель доступна только админам доверенных чатов.")
    from services.runtime_settings import effective_allowed_ids

    if not effective_allowed_ids():
        print("⚠️  Whitelist пуст — доступ к подаче закрыт для всех (fail-closed).")

    print()
    if failures:
        print(f"{FAIL} Проблемы: {', '.join(failures)}")
        return 1
    print(f"{OK} Всё готово к запуску.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
