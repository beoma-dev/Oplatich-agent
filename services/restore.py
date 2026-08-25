"""Восстановление данных из архива, загруженного в Mini App.

Операция разрушительная: она пишет поверх живых данных. Поэтому здесь
больше проверок, чем кода по делу, и порядок их такой:

1. Архив не разворачивается «куда положат». `extractall(filter="data")`
   отбивает абсолютные пути, `..`, симлинки и спецфайлы — tar-архив
   с `../../etc/` иначе разложился бы мимо каталога данных.
2. Состав сверяется с ожидаемым. Чужой tar.gz — не «пустое
   восстановление», а отказ: молча обнулить настройки хуже, чем не
   восстановиться.
3. Содержимое читается ДО подмены: база должна открываться, настройки —
   разбираться. Половина восстановления хуже, чем его отсутствие.
4. Перед подменой снимается свой бэкап текущего состояния. Возврат должен
   существовать даже тогда, когда человек загрузил не тот файл.

Двухшаговость (сначала `inspect`, потом `apply`) — не формальность:
человек видит дату архива и что в нём, прежде чем соглашаться. Ошибка
«не тот файл» ловится глазами лучше, чем любой проверкой.

`conversations.pickle` намеренно НЕ восстанавливается: это незаконченные
диалоги в чате, они живут в памяти работающего бота и будут перезаписаны
им же. Подменять их под ним — единственный способ получить заявку,
собранную из двух разных разговоров.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from services import backup

log = logging.getLogger(__name__)

# Потолок на загрузку. Боевой архив — единицы мегабайт; всё, что сильно
# больше, скорее ошибка или попытка забить диск, чем бэкап.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

# Что мы кладём в архив и, значит, готовы принять обратно.
DB_NAME = "security.db"
SETTINGS_NAME = "bot_settings.json"
DIRECTORY_NAME = "known_users.json"
STORAGE_NAME = "storage"
SKIPPED = "conversations.pickle"
KNOWN = {DB_NAME, SETTINGS_NAME, DIRECTORY_NAME, STORAGE_NAME, SKIPPED}


class RestoreError(Exception):
    """Архив нельзя принять. Сообщение предназначено человеку."""


def _unpack(blob: bytes, into: Path) -> None:
    """Разворачивает архив, отбивая всё, что не похоже на наш бэкап."""
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise RestoreError(
            f"Архив больше {MAX_ARCHIVE_BYTES // (1024 * 1024)} МБ — это не наш бэкап."
        )
    raw = into / "upload.tar.gz"
    raw.write_bytes(blob)
    try:
        with tarfile.open(raw, "r:gz") as tar:
            names = tar.getnames()
            stray = {n.split("/", 1)[0] for n in names} - KNOWN
            if stray:
                raise RestoreError(
                    "В архиве посторонние файлы: " + ", ".join(sorted(stray))
                    + ". Похоже, это не бэкап Оплатыча."
                )
            tar.extractall(into, filter="data")
    except tarfile.TarError as exc:
        raise RestoreError(f"Архив не читается: {exc}") from exc
    finally:
        raw.unlink(missing_ok=True)


def _summary(root: Path) -> dict:
    """Что внутри развёрнутого архива. Заодно проверка, что оно живое."""
    db = root / DB_NAME
    if not db.exists():
        raise RestoreError(f"В архиве нет {DB_NAME} — восстанавливать нечего.")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "select name from sqlite_master where type='table'")}
            counts = {}
            for name in ("requests", "audit"):
                if name in tables:
                    counts[name] = conn.execute(f"select count(*) from {name}").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RestoreError(f"База из архива не открывается: {exc}") from exc

    cfg_path = root / SETTINGS_NAME
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RestoreError(f"Настройки из архива не разбираются: {exc}") from exc

    storage = root / STORAGE_NAME
    files = sum(1 for p in storage.rglob("*") if p.is_file()) if storage.is_dir() else 0
    stamp = datetime.fromtimestamp(
        db.stat().st_mtime, ZoneInfo(settings.timezone)
    ).strftime("%d.%m.%Y %H:%M")
    return {
        "made_at": stamp,
        "requests": counts.get("requests", 0),
        "audit": counts.get("audit", 0),
        "files": files,
        "financiers": len(cfg.get("finance", [])),
        "allowed": len(cfg.get("allowed", [])),
        "has_settings": bool(cfg),
    }


def inspect_sync(blob: bytes) -> dict:
    """Что в архиве. Ничего не меняет — это шаг «покажи, прежде чем ставить»."""
    with tempfile.TemporaryDirectory(prefix="restore-check-") as tmp:
        root = Path(tmp)
        _unpack(blob, root)
        return _summary(root)


def apply_sync(blob: bytes) -> dict:
    """Ставит архив поверх данных. Возвращает сводку и путь к своей копии."""
    with tempfile.TemporaryDirectory(prefix="restore-apply-") as tmp:
        root = Path(tmp)
        _unpack(blob, root)
        summary = _summary(root)

        # Возврат обязан существовать до того, как мы что-то испортим.
        safety = backup.create_backup_sync()
        log.warning("Восстановление из архива: страховочная копия %s", safety.name)

        data_dir = settings.security_db_path.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        for name, target in (
            (DB_NAME, settings.security_db_path),
            (SETTINGS_NAME, settings.runtime_settings_path),
            (DIRECTORY_NAME, settings.user_directory_path),
        ):
            src = root / name
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)

        src_storage = root / STORAGE_NAME
        if src_storage.is_dir():
            live = settings.storage_path
            if live.exists():
                shutil.rmtree(live)
            shutil.copytree(src_storage, live)

        summary["safety_backup"] = safety.name
        log.warning(
            "Данные восстановлены из архива: заявок %s, файлов %s",
            summary["requests"], summary["files"],
        )
        return summary


def reload_caches() -> None:
    """Сбрасывает то, что бот держит в памяти, — чтобы не нужен был рестарт.

    Соединения к SQLite открываются на каждую операцию, поэтому подменённый
    файл базы подхватывается сам. А вот настройки и справочник пользователей
    закэшированы в модулях, и без сброса бот продолжил бы работать по старым.
    """
    from services import runtime_settings as rs
    from services import user_directory

    rs._cache = None
    user_directory._cache = None
