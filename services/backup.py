"""Автобэкап данных: реестр, файлы счетов, SQLite аудита, настройки.

Ежедневно в BACKUP_TIME (в таймзоне приложения) собирается tar.gz со всеми
данными, хранится последних BACKUP_KEEP копий в data/backups и отправляется
админам в Telegram (лимит Bot API — до ~50 МБ; при превышении приходит
предупреждение с путём к файлу). Ручной запуск — команда /backup.
"""
from __future__ import annotations

import asyncio
import logging
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot

from config import settings
from services import alerts
from services import runtime_settings as rs
from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Как часто цикл перечитывает настройки: правки из админ-панели
# применяются без рестарта в пределах этого интервала.
CHECK_INTERVAL = 300.0

_PREFIX = "invoice-bot-backup-"
# Отправляем файлом только до этого размера (лимит Bot API ~50 МБ).
MAX_SEND_BYTES = 45 * 1024 * 1024


def backup_dir() -> Path:
    return settings.security_db_path.parent / "backups"


def _rotate_sync(keep: int) -> None:
    backups = sorted(backup_dir().glob(f"{_PREFIX}*.tar.gz"))
    for old in backups[:-keep] if keep > 0 else backups:
        try:
            old.unlink()
            log.info("Бэкап-ротация: удалён %s", old.name)
        except OSError:
            log.warning("Не удалось удалить старый бэкап %s", old.name)


def create_backup_sync() -> Path:
    """Собирает tar.gz со всеми данными. Возвращает путь к архиву."""
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(ZoneInfo(settings.timezone))
    target = target_dir / f"{_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    i = 1
    while target.exists():  # два бэкапа в одну секунду (ручной + плановый)
        target = target_dir / f"{_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}-{i}.tar.gz"
        i += 1

    members: list[tuple[Path, str]] = [
        (settings.storage_path, "storage"),
        (settings.security_db_path, settings.security_db_path.name),
        (settings.runtime_settings_path, settings.runtime_settings_path.name),
        (settings.user_directory_path, settings.user_directory_path.name),
        (
            settings.security_db_path.parent / "conversations.pickle",
            "conversations.pickle",
        ),
    ]
    with tarfile.open(target, "w:gz") as tar:
        for path, arcname in members:
            if path.exists():
                tar.add(path, arcname=arcname)

    _rotate_sync(rs.backup_config()["keep"])
    log.info("Бэкап собран: %s (%d КБ)", target.name, target.stat().st_size // 1024)
    return target


async def run_backup(bot: Bot) -> tuple[Path, int]:
    """Собирает бэкап и рассылает его админам. Возвращает (путь, доставлено)."""
    path = await asyncio.to_thread(create_backup_sync)
    size = path.stat().st_size

    if size > MAX_SEND_BYTES:
        await alerts.alert_admins(
            bot,
            "Бэкап собран, но слишком велик для Telegram",
            f"{path} · {size // (1024 * 1024)} МБ — заберите с сервера",
            signature="backup-too-big",
        )
        return path, 0

    data = await asyncio.to_thread(path.read_bytes)
    delivered = 0
    for admin_id in effective_admin_ids():
        try:
            await bot.send_document(
                chat_id=admin_id,
                document=data,
                filename=path.name,
                caption=f"💾 Бэкап данных · {max(size // 1024, 1)} КБ",
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — не срываем остальных получателей
            log.warning("Не удалось отправить бэкап админу %s", admin_id)
    return path, delivered


def _seconds_until(hhmm: str, now: datetime) -> float:
    """Секунды до ближайшего времени hhmm (сегодня или завтра)."""
    hour, minute = (int(p) for p in hhmm.strip().split(":", 1))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def backup_loop(bot: Bot) -> None:
    """Фоновая задача: ежедневный бэкап по расписанию из настроек.

    Конфигурация (вкл/выкл, время, копий) читается каждые CHECK_INTERVAL
    секунд из runtime_settings — правки из админ-панели ⚙️ применяются
    без рестарта. Цикл переживает любые сбои.
    """
    log.info("Планировщик бэкапа запущен: %s", rs.backup_config())
    while True:
        cfg = rs.backup_config()
        if not cfg["enabled"]:
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        try:
            delay = _seconds_until(
                cfg["time"], datetime.now(ZoneInfo(settings.timezone))
            )
        except (ValueError, IndexError):
            log.error("Некорректное время бэкапа %r — жду исправления", cfg["time"])
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        if delay > CHECK_INTERVAL:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        await asyncio.sleep(delay)
        if not rs.backup_config()["enabled"]:  # выключили, пока ждали
            continue
        try:
            await run_backup(bot)
        except Exception:  # noqa: BLE001 — цикл должен пережить любой сбой
            log.exception("Сбой планового бэкапа")
            await alerts.alert_admins(
                bot, "Сбой планового бэкапа", "детали в логах", signature="backup-failed"
            )
        await asyncio.sleep(61)  # не сработать дважды в ту же минуту
