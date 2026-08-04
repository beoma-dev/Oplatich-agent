"""Сохранение файла счёта в локальный каталог (вместо Google Drive)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)


def build_invoice_filename(
    original_name: str, counterparty: str, amount: Decimal, now: datetime
) -> str:
    """Имя файла счёта вида 20260803_Ромашка_125000.pdf.

    Имя строится ТОЛЬКО из безопасных символов: расширение чистится до
    [a-z0-9] (не более 8 знаков), контрагент — до букв/цифр/пробела/дефиса.
    Это исключает path traversal через имя файла или «расширение» с «/».
    """
    ext_raw = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "bin"
    ext = re.sub(r"[^a-z0-9]", "", ext_raw)[:8] or "bin"
    safe_cp = "".join(c for c in counterparty if c.isalnum() or c in " _-").strip()
    return f"{now.strftime('%Y%m%d')}_{safe_cp or 'invoice'}_{amount:.0f}.{ext}"


def _save_sync(*, content: bytes, filename: str) -> Path:
    """Сохраняет файл в STORAGE_DIR. При коллизии имени добавляет суффикс.

    Возвращает абсолютный путь к сохранённому файлу.
    """
    target_dir = settings.storage_path
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / filename
    # Не перезаписываем существующий файл — добавляем _1, _2, ...
    if path.exists():
        stem, suffix = path.stem, path.suffix
        i = 1
        while path.exists():
            path = target_dir / f"{stem}_{i}{suffix}"
            i += 1

    path.write_bytes(content)
    log.info("Файл счёта сохранён: %s", path)
    return path.resolve()


async def save_invoice_file(*, content: bytes, filename: str) -> Path:
    """Асинхронная обёртка над сохранением файла."""
    return await asyncio.to_thread(_save_sync, content=content, filename=filename)
