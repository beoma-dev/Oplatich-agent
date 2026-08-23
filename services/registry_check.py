"""Сверка первичного реестра с xlsx-зеркалом.

Истина живёт в реестре (SQLite или Google-таблица), но финансисты каждый
день открывают именно зеркало. Запись в зеркало намеренно вторична: её сбой
не отменяет принятую заявку, а только пишется в лог. Значит, разойтись эти
двое могут молча — и один раз уже разошлись: в реестре было пусто, а в
файле оставались две строки от старых удалённых заявок.

Сверка сравнивает состав по «ID заявки» и делает расхождение видимым:
в карточке «Здоровье бота» и алертом админам после каждого бэкапа.
"""
from __future__ import annotations

import asyncio
import logging

import openpyxl

from bot.models import SHEET_HEADERS
from services import alerts, storage

log = logging.getLogger(__name__)

_ID_COL = SHEET_HEADERS.index("ID заявки")
# Сколько идентификаторов показываем в отчёте: остальное — числом.
SHOW_IDS = 5


def _mirror_ids_sync(path) -> tuple[list[str], int]:
    """ID и число непустых строк зеркала. read_only — файл бывает большим."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        ids: list[str] = []
        rows = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(v not in (None, "") for v in row):
                continue
            rows += 1
            value = row[_ID_COL] if len(row) > _ID_COL else None
            if value not in (None, ""):
                ids.append(str(value).strip())
        return ids, rows
    finally:
        wb.close()


async def check() -> dict:
    """Сверяет реестр с зеркалом.

    checked=False — сверять нечего (зеркало не настроено или файла ещё нет);
    это не ошибка, а штатное состояние локальной установки без xlsx.
    """
    path = storage.registry_export_path()
    if path is None:
        return {"checked": False, "reason": "зеркало не настроено", "ok": True}
    if not path.exists():
        return {"checked": False, "reason": "файла зеркала ещё нет", "ok": True}

    try:
        primary = set(await storage.all_request_ids())
        mirror_ids, mirror_rows = await asyncio.to_thread(_mirror_ids_sync, path)
    except Exception as exc:  # noqa: BLE001 — сверка не должна ломать панель
        log.exception("Сверка реестра и зеркала не удалась")
        return {"checked": False, "reason": f"не удалось прочитать: {exc}", "ok": True}

    mirror = set(mirror_ids)
    # Строки без ID — старый формат: сверить их не с чем, но и молчать о них
    # нельзя, иначе «лишние две строки» так и останутся невидимыми.
    without_id = mirror_rows - len(mirror_ids)
    missing = sorted(primary - mirror)
    extra = sorted(mirror - primary)
    return {
        "checked": True,
        "reason": "",
        "primary": len(primary),
        "mirror": mirror_rows,
        "missing_in_mirror": missing[:SHOW_IDS],
        "missing_count": len(missing),
        "extra_in_mirror": extra[:SHOW_IDS],
        "extra_count": len(extra),
        "rows_without_id": without_id,
        "ok": not missing and not extra and not without_id,
    }


def describe(result: dict) -> str:
    """Человеческая строка для панели и алерта."""
    if not result.get("checked"):
        return f"Сверка не проводилась: {result.get('reason', 'причина неизвестна')}."
    if result.get("ok"):
        return f"Реестр и зеркало совпадают: {result['primary']} заявок."
    parts = []
    if result["missing_count"]:
        parts.append(f"нет в зеркале — {result['missing_count']}")
    if result["extra_count"]:
        parts.append(f"лишних в зеркале — {result['extra_count']}")
    if result["rows_without_id"]:
        parts.append(f"строк без ID — {result['rows_without_id']}")
    return (
        f"Реестр и зеркало разошлись: {', '.join(parts)}. "
        f"В реестре {result['primary']}, в файле {result['mirror']}."
    )


async def alert_if_diverged(bot) -> dict:
    """Сверяет и, если разошлись, сообщает админам. Возвращает результат."""
    result = await check()
    if result.get("checked") and not result.get("ok"):
        details = describe(result)
        ids = result["missing_in_mirror"] + result["extra_in_mirror"]
        if ids:
            details += " Например: " + ", ".join(ids)
        try:
            await alerts.alert_admins(
                bot,
                "Реестр и xlsx-зеркало разошлись",
                details,
                signature="registry-mirror-diverged",
                kind="mirror",
            )
        except Exception:  # noqa: BLE001 — алерт не должен ломать сверку
            log.exception("Не удалось сообщить о расхождении реестра")
    return result
