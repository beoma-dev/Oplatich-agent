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
from services import alerts, dns_pin, registry_check, tg_retry
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
            kind="backup",
        )
        return path, 0

    data = await asyncio.to_thread(path.read_bytes)
    delivered = 0
    for admin_id in effective_admin_ids():
        try:
            # Длинные паузы: архив уходит раз в сутки и его никто не ждёт
            # на экране, а копия «вне сервера» — это и есть весь смысл
            # бэкапа. Мегабайты через нестабильный канал падают чаще
            # коротких сообщений, поэтому терпение здесь оправдано.
            await tg_retry.send_with_retry(
                lambda aid=admin_id: bot.send_document(
                    chat_id=aid,
                    document=data,
                    filename=path.name,
                    caption=f"💾 Бэкап данных · {max(size // 1024, 1)} КБ",
                ),
                what=f"Бэкап админу {admin_id}",
                pauses=tg_retry.PATIENT_PAUSES,
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — не срываем остальных получателей
            log.warning("Не удалось отправить бэкап админу %s", admin_id)
    return path, delivered


async def alert_if_pin_stale(bot: Bot) -> bool:
    """Сверяет прибитый IPv4 Telegram с DNS. True — всё совпадает."""
    ok, message = await asyncio.to_thread(dns_pin.check)
    if ok:
        log.info("Пин адреса Telegram: %s", message)
        return True
    log.error("Пин адреса Telegram протух: %s", message)
    await alerts.alert_admins(
        bot,
        "Адрес Telegram сменился, а пин остался старым",
        message,
        signature="telegram-pin-stale",
        kind="telegram",
        hint="Пока не поправите, бот стучится в никуда и замолчит целиком.",
    )
    return False


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
            # Раз в сутки заодно сверяем реестр с зеркалом: обе задачи про
            # целостность данных, и второго планировщика ради этого заводить
            # незачем. Выключен бэкап — сверка остаётся в админ-панели.
            await registry_check.alert_if_diverged(bot)
            # И заодно — не протух ли прибитый адрес Telegram. Пин молчит,
            # пока Telegram не сменит IP; сверка с живой DNS-выдачей — та
            # единственная причина, по которой пин вообще допустим.
            await alert_if_pin_stale(bot)
        except Exception:  # noqa: BLE001 — цикл должен пережить любой сбой
            log.exception("Сбой планового бэкапа")
            await alerts.alert_admins(
                bot,
                "Сбой планового бэкапа",
                "детали в логах",
                signature="backup-failed",
                kind="backup",
                hint="Проверьте место на диске и журнал контейнера app.",
            )
        await asyncio.sleep(61)  # не сработать дважды в ту же минуту
