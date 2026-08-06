"""Фасад хранилища: единая точка для реестра и файлов счетов.

Переключение бэкенда — STORAGE_BACKEND в .env:
  local  — SQLite (источник правды: транзакции, статусы, выборки) +
           красивый xlsx как живое зеркало/экспорт;
  google — Google Sheets + Google Drive (service account, доступ по правам
           на таблицу/папку) + опциональное xlsx-зеркало
           (REGISTRY_XLSX_FILE).

Ошибки зеркала не срывают сценарий (авторитет — первичное хранилище);
доступ к xlsx сериализуется общей блокировкой.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.models import InvoiceRequest
from config import settings
from services import google_backend, local_storage, registry_sqlite, registry_xlsx

log = logging.getLogger(__name__)

_xlsx_lock = asyncio.Lock()


def _mirror_path() -> Path | None:
    """xlsx-зеркало: local — основной файл реестра, google — REGISTRY_XLSX_FILE."""
    if settings.storage_is_google:
        return settings.registry_xlsx_path
    return settings.registry_path


def registry_export_path() -> Path | None:
    """Путь к актуальному xlsx для выгрузки админом (/export)."""
    return _mirror_path()


async def _mirror_append(request: InvoiceRequest) -> None:
    path = _mirror_path()
    if path is None:
        return
    try:
        async with _xlsx_lock:
            await asyncio.to_thread(registry_xlsx.append_sync, request, path)
    except Exception:  # noqa: BLE001 — зеркало вторично
        log.exception("xlsx-зеркало: не удалось записать заявку %s", request.request_id)


async def _mirror_status(request_id: str, status_text: str) -> None:
    path = _mirror_path()
    if path is None:
        return
    try:
        async with _xlsx_lock:
            await asyncio.to_thread(
                registry_xlsx.set_status_sync, request_id, status_text, path
            )
    except Exception:  # noqa: BLE001
        log.exception("xlsx-зеркало: не удалось обновить статус %s", request_id)


async def save_invoice(content: bytes, filename: str) -> str:
    """Сохраняет файл счёта. Возвращает значение для колонки «Ссылка на счет».

    google → webViewLink в Drive (доступ по правам на папку);
    local  → абсолютный путь в STORAGE_DIR.
    """
    if settings.storage_is_google:
        return await asyncio.to_thread(
            google_backend.upload_invoice_file_sync, content, filename
        )
    path = await local_storage.save_invoice_file(content=content, filename=filename)
    return str(path)


async def save_artifact(content: bytes, filename: str) -> None:
    """Сохраняет служебный артефакт (PDF заявки) в текущий бэкенд."""
    if settings.storage_is_google:
        await asyncio.to_thread(google_backend.upload_invoice_file_sync, content, filename)
    else:
        await local_storage.save_invoice_file(content=content, filename=filename)


async def append_invoice(request: InvoiceRequest) -> int:
    """Пишет заявку в первичное хранилище + xlsx-зеркало."""
    if settings.storage_is_google:
        row = await asyncio.to_thread(google_backend.append_invoice_sync, request)
    else:
        row = await asyncio.to_thread(registry_sqlite.append_sync, request)
    await _mirror_append(request)
    return row


async def set_request_status(request_id: str, status_text: str) -> dict[str, str] | None:
    """Меняет статус заявки в первичном хранилище + xlsx-зеркале."""
    if settings.storage_is_google:
        row = await asyncio.to_thread(
            google_backend.set_status_sync, request_id, status_text
        )
    else:
        row = await asyncio.to_thread(
            registry_sqlite.set_status_sync, request_id, status_text
        )
    if row is not None:
        await _mirror_status(request_id, status_text)
    return row


async def counterparty_book(limit: int = 6) -> list[dict[str, str]]:
    """Справочник контрагентов для подсказок: [{name, requisites}, …]."""
    try:
        if settings.storage_is_google:
            return await asyncio.to_thread(google_backend.counterparty_book_sync, limit)
        return await asyncio.to_thread(registry_sqlite.counterparty_book_sync, limit)
    except Exception:  # noqa: BLE001 — подсказки не критичны
        log.exception("Не удалось получить справочник контрагентов")
        return []


async def get_request(request_id: str) -> dict[str, str] | None:
    """Заявка по ID из первичного хранилища (в формате SHEET_HEADERS)."""
    if settings.storage_is_google:
        return await asyncio.to_thread(google_backend.get_request_sync, request_id)
    return await asyncio.to_thread(registry_sqlite.get_request_sync, request_id)


async def delete_request(request_id: str) -> bool:
    """Удаляет заявку из первичного хранилища + xlsx-зеркала.

    Возвращает True, если заявка была найдена и удалена. Зеркало вторично:
    его сбой не отменяет удаление в источнике правды.
    """
    if settings.storage_is_google:
        removed = await asyncio.to_thread(google_backend.delete_request_sync, request_id)
    else:
        removed = await asyncio.to_thread(registry_sqlite.delete_sync, request_id)
    if removed:
        path = _mirror_path()
        if path is not None:
            try:
                async with _xlsx_lock:
                    await asyncio.to_thread(registry_xlsx.delete_sync, request_id, path)
            except Exception:  # noqa: BLE001 — зеркало вторично
                log.exception("xlsx-зеркало: не удалось удалить заявку %s", request_id)
    return removed


class RegistryUnavailable(RuntimeError):
    """Реестр не прочитался: сеть, квота Google или таймаут.

    Отдельный тип, потому что «реестр недоступен» и «заявок нет» — разные
    вещи: на пустом списке напоминания молча не уходили, и понять, что
    именно случилось, было нельзя.
    """


async def recent_requests(limit: int = 200, *, strict: bool = False) -> list[dict[str, str]]:
    """Последние заявки всех авторов — панель финансиста.

    strict=True — поднять RegistryUnavailable вместо пустого списка. Нужен
    тем, для кого «пусто» означает «делать нечего»: напоминаниям.
    """
    try:
        if settings.storage_is_google:
            return await asyncio.to_thread(google_backend.recent_requests_sync, limit)
        return await asyncio.to_thread(registry_sqlite.recent_requests_sync, limit)
    except Exception as exc:  # noqa: BLE001 — панель не критична
        log.exception("Не удалось получить список заявок")
        if strict:
            raise RegistryUnavailable(str(exc)) from exc
        return []


async def recent_by_author(
    telegram_id: int, limit: int = 10, *, strict: bool = False
) -> list[dict[str, str]]:
    """Последние заявки автора — для экрана «Мои заявки».

    strict=True — поднять RegistryUnavailable вместо пустого списка. Иначе
    недоступный реестр читается как «заявок нет», то есть человеку показывают
    неправду: та же ошибка, что однажды съела напоминания.
    """
    try:
        if settings.storage_is_google:
            return await asyncio.to_thread(
                google_backend.recent_by_author_sync, telegram_id, limit
            )
        return await asyncio.to_thread(
            registry_sqlite.recent_by_author_sync, telegram_id, limit
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось получить заявки автора")
        if strict:
            raise RegistryUnavailable(str(exc)) from exc
        return []
