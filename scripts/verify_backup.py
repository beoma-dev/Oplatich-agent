#!/usr/bin/env python3
"""Разворачивает последний бэкап во временный каталог и проверяет его.

Бэкап, который ни разу не разворачивали, — это гипотеза, а не бэкап. Скрипт
превращает проверку в одну команду: распаковать, прочитать реестр и базу,
сверить их между собой, убедиться, что у каждой заявки на месте её PDF.

    docker compose run --rm app python scripts/verify_backup.py

Ничего не меняет: работает с копией во временном каталоге, который удаляет
за собой. Прогонять после каждого изменения состава данных и раз в квартал
просто так — иначе однажды выяснится, что архив собирался пустым.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402

from bot.models import SHEET_HEADERS  # noqa: E402
from config import settings  # noqa: E402
from services.backup import backup_dir  # noqa: E402

_ID_COL = SHEET_HEADERS.index("ID заявки")
_failures = 0


def _say(good: bool, text: str) -> None:
    global _failures
    if not good:
        _failures += 1
    print(("  ✓ " if good else "  ✗ ") + text)


def _latest_backup() -> Path | None:
    archives = sorted(
        backup_dir().glob("*.tar.gz"), key=lambda p: p.stat().st_mtime
    )
    return archives[-1] if archives else None


def _check_registry(root: Path) -> list[str]:
    """ID заявок из реестра. Пустой список — либо пусто, либо не прочиталось."""
    # Имя файла берём из настроек, а не из константы: на google-бэкенде
    # зеркало называется как задано в REGISTRY_XLSX_FILE, и проверка,
    # прибитая к «registry.xlsx», молча читала СТАРЫЙ файл, оставшийся от
    # локального режима, и рапортовала «2 заявки» при восьми в реестре.
    configured = settings.registry_xlsx_path or settings.registry_path
    path = root / "storage" / configured.name
    if not path.exists():
        # На google-бэкенде реестр живёт в таблице, а xlsx-зеркало включается
        # отдельно (REGISTRY_XLSX_FILE). Его отсутствие — норма, а не провал:
        # проверка, которая кричит на исправном архиве, перестаёт читаться.
        if settings.storage_is_google or not settings.registry_xlsx_file:
            print("  · xlsx-реестра в архиве нет — так и задумано на этом бэкенде")
            return []
        _say(False, "реестра в архиве нет")
        return []
    try:
        ws = openpyxl.load_workbook(path, read_only=True).active
        rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
                if any(v not in (None, "") for v in r)]
        ids = [str(r[_ID_COL]).strip() for r in rows
               if len(r) > _ID_COL and r[_ID_COL] not in (None, "")]
        _say(True, f"реестр читается: {len(rows)} заявок")
        _say(len(ids) == len(rows), f"у всех строк есть ID заявки ({len(ids)} из {len(rows)})")
        return ids
    except Exception as exc:  # noqa: BLE001
        _say(False, f"реестр НЕ читается: {exc}")
        return []


def _check_database(root: Path, registry_ids: list[str]) -> None:
    path = root / "security.db"
    if not path.exists():
        _say(False, "базы в архиве нет")
        return
    try:
        conn = sqlite3.connect(path)
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("requests", "audit", "dedup", "finance_cards")
        }
        _say(True, f"база открывается: {counts}")
        _say(
            conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "integrity_check пройден",
        )
        db_ids = {r[0] for r in conn.execute("SELECT request_id FROM requests")}
        if settings.storage_is_google:
            # Источник правды — Google-таблица, SQLite-реестр там не ведётся.
            # Сверять его с зеркалом бессмысленно: расхождение будет всегда,
            # а проверка, которая всегда красная, перестаёт читаться.
            print(f"  · SQLite-реестр не ведётся на google-бэкенде "
                  f"({len(db_ids)} стар. строк) — сверять не с чем")
        else:
            _say(
                db_ids == set(registry_ids),
                f"реестр и база сходятся: {len(db_ids)} против {len(registry_ids)}",
            )
    except Exception as exc:  # noqa: BLE001
        _say(False, f"база НЕ открывается: {exc}")


def _check_settings(root: Path) -> None:
    path = root / "bot_settings.json"
    if not path.exists():
        _say(False, "настроек в архиве нет")
        return
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        _say(isinstance(cfg, dict), f"настройки читаются: ключей {len(cfg)}")
        _say(
            "finance" in cfg and "admins" in cfg,
            "состав финансистов и админов на месте",
        )
    except Exception as exc:  # noqa: BLE001
        _say(False, f"настройки НЕ читаются: {exc}")


def _check_files(root: Path, registry_ids: list[str]) -> None:
    storage = root / "storage"
    pdfs = {p.stem for p in storage.glob("INV-*.pdf")}
    missing = [i for i in registry_ids if i not in pdfs]
    if settings.storage_is_google:
        # PDF и счета уходят в Google Drive, локально их нет — и в архиве
        # тоже. Молчать об этом нельзя: человек, читающий отчёт, должен
        # знать, что документы бэкапом НЕ покрыты.
        print(f"  · PDF в архиве: {len(pdfs)} (документы живут в Google Drive "
              f"и в этот архив не попадают)")
    else:
        _say(
            not missing,
            f"PDF заявок: {len(pdfs)} файлов"
            + (f"; НЕТ для {', '.join(missing[:3])}" if missing else ""),
        )
    # Сам xlsx-реестр лежит в том же каталоге и файлом счёта не является.
    # Раньше он вычитался всегда, и без него счётчик уходил в минус.
    others = [
        p for p in storage.iterdir()
        if p.is_file() and not p.name.startswith("INV-")
        and not p.name.endswith(".xlsx")
    ]
    _say(True, f"файлов счетов от пользователей: {len(others)}")


def main() -> int:
    archive = _latest_backup()
    if archive is None:
        print(f"Бэкапов нет в {backup_dir()} — проверять нечего.")
        return 1
    size = archive.stat().st_size
    print(f"Проверяю {archive.name} ({size // 1024} КБ)\n")

    tmp = Path(tempfile.mkdtemp(prefix="verify-backup-"))
    try:
        try:
            with tarfile.open(archive) as tar:
                # filter="data" отбивает абсолютные пути, «..», симлинки и
                # спецфайлы. Архив свой, но проверяют им и тот, что принесли
                # со стороны, — а распаковка без фильтра как раз и есть та
                # дыра, от которой закрыт services/restore.
                tar.extractall(tmp, filter="data")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ архив не распаковался: {exc}")
            return 1
        _say(True, "архив распаковался")
        ids = _check_registry(tmp)
        _check_database(tmp, ids)
        _check_settings(tmp)
        _check_files(tmp, ids)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"ИТОГ: проблем — {_failures}. Восстановление под вопросом.")
        return 1
    print("ИТОГ: восстановление возможно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
