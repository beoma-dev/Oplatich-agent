"""Правила дат, fail-closed доступ, справочник ФИО, анти-формулы."""
from __future__ import annotations

from datetime import date

import pytest

from bot.access import is_allowed
from bot.models import excel_safe
from bot.scheduling import auto_planned_date, next_business_day
from config import settings
from services import runtime_settings as rs


class TestScheduling:
    def test_urgent_is_today_even_on_weekend(self):
        saturday = date(2026, 8, 8)
        assert auto_planned_date(True, today=saturday) == saturday

    def test_normal_midweek_is_next_day(self):
        tuesday = date(2026, 8, 4)
        assert auto_planned_date(False, today=tuesday) == date(2026, 8, 5)

    def test_friday_rolls_to_monday(self):
        friday = date(2026, 8, 7)
        assert auto_planned_date(False, today=friday) == date(2026, 8, 10)

    def test_weekend_rolls_to_monday(self):
        assert auto_planned_date(False, today=date(2026, 8, 8)) == date(2026, 8, 10)
        assert auto_planned_date(False, today=date(2026, 8, 9)) == date(2026, 8, 10)

    def test_next_business_day_skips_weekend(self):
        assert next_business_day(date(2026, 8, 7)) == date(2026, 8, 10)
        assert next_business_day(date(2026, 8, 10)) == date(2026, 8, 11)


class TestFailClosedAccess:
    def test_empty_whitelist_denies_everyone(self, tmp_paths):
        assert not is_allowed(12345)

    def test_admin_passes_even_with_empty_whitelist(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("admin_ids", None)
        monkeypatch.setattr(settings, "admin_ids_raw", "42")
        assert is_allowed(42)
        assert not is_allowed(43)

    def test_whitelisted_user_passes(self, tmp_paths):
        rs.add_allowed(777)
        assert is_allowed(777)
        assert not is_allowed(778)


class TestEmployeeNames:
    def test_parse_and_lookup(self, monkeypatch):
        settings.__dict__.pop("employee_names", None)
        monkeypatch.setattr(
            settings, "employee_names_raw",
            "969015071:Елипашев Павел, 123:Иванов Иван Иванович,битая-запись",
        )
        assert settings.employee_name_for(969015071) == "Елипашев Павел"
        assert settings.employee_name_for(123) == "Иванов Иван Иванович"
        assert settings.employee_name_for(999) is None
        settings.__dict__.pop("employee_names", None)


class TestExcelSafe:
    def test_formula_prefixes_escaped(self):
        assert excel_safe("=IMPORTRANGE(...)") == "'=IMPORTRANGE(...)"
        assert excel_safe("+7 999 111-22-33") == "'+7 999 111-22-33"
        assert excel_safe("-минус") == "'-минус"
        assert excel_safe("@упоминание") == "'@упоминание"

    def test_normal_values_untouched(self):
        assert excel_safe("ООО «Ромашка»") == "ООО «Ромашка»"
        assert excel_safe("125000.50") == "125000.50"
        assert excel_safe("") == ""



def test_tests_never_touch_the_projects_own_data_dir():
    """Рабочие пути тестов не должны указывать в каталог проекта.

    Репозиторий бывает развёрнут на сервере рядом с боевым ботом, и
    tmp_paths спасает только тех, кто её попросил. 25.08.2026 тест пульса
    без изоляции записал «ошибку» в настоящий журнал инцидентов, а файл,
    переписанный от root, стал боту недоступен на запись. Страховка теперь
    глобальная — этот тест сторожит, что её не сняли.
    """
    from pathlib import Path

    from config import settings

    project = Path(__file__).resolve().parent.parent
    for name in (
        settings.runtime_settings_path,
        settings.security_db_path,
        settings.user_directory_path,
        settings.storage_path,
    ):
        assert project not in Path(name).resolve().parents, (
            f"{name} ведёт в каталог проекта — тесты будут писать в боевые данные"
        )


class TestRestoreArchive:
    """Восстановление из загруженного архива — операция разрушительная.

    Проверяем не «работает ли», а «не сработает ли там, где не должно»:
    tar с путями наружу, чужой архив, битый файл. Ошибка здесь пишет
    поверх боевых данных, поэтому отказ обязан быть громким и полным.
    """

    def _archive(self, tmp_path, members: dict[str, bytes]) -> bytes:
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def _good_db(self, tmp_path):
        import sqlite3

        path = tmp_path / "security.db"
        conn = sqlite3.connect(path)
        conn.execute("create table requests (id integer primary key)")
        conn.execute("create table audit (id integer primary key)")
        conn.commit()
        conn.close()
        return path.read_bytes()

    def test_paths_outside_the_archive_are_refused(self, tmp_path, tmp_paths):
        """tar с «../» не должен раскладываться мимо каталога данных."""
        from services import restore

        blob = self._archive(tmp_path, {"../../etc/passwd": b"root"})
        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect_sync(blob)
        assert "посторонние" in str(exc.value).lower()

    def test_foreign_archive_is_refused_not_silently_applied(self, tmp_path, tmp_paths):
        """Чужой tar.gz — отказ. Молча обнулить настройки хуже, чем не встать."""
        from services import restore

        blob = self._archive(tmp_path, {"holiday-photos.jpg": b"\xff\xd8"})
        with pytest.raises(restore.RestoreError):
            restore.inspect_sync(blob)

    def test_broken_archive_is_refused(self, tmp_paths):
        from services import restore

        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect_sync(b"not a gzip at all")
        assert "не читается" in str(exc.value)

    def test_archive_without_database_is_refused(self, tmp_path, tmp_paths):
        from services import restore

        blob = self._archive(tmp_path, {"bot_settings.json": b"{}"})
        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect_sync(blob)
        assert "security.db" in str(exc.value)

    def test_inspect_reports_contents_and_changes_nothing(self, tmp_path, tmp_paths):
        from config import settings
        from services import restore

        before = settings.runtime_settings_path.read_bytes() \
            if settings.runtime_settings_path.exists() else None
        blob = self._archive(tmp_path, {
            "security.db": self._good_db(tmp_path),
            "bot_settings.json": b'{"finance": ["1", "2"], "allowed": [7]}',
        })
        summary = restore.inspect_sync(blob)
        assert summary["financiers"] == 2 and summary["allowed"] == 1
        after = settings.runtime_settings_path.read_bytes() \
            if settings.runtime_settings_path.exists() else None
        assert after == before, "«посмотреть» не должно ничего менять"

    def test_apply_makes_a_safety_copy_first(self, tmp_path, tmp_paths):
        """Возврат обязан существовать даже при загрузке не того файла."""
        from services import backup, restore

        blob = self._archive(tmp_path, {
            "security.db": self._good_db(tmp_path),
            "bot_settings.json": b'{"finance": ["9"]}',
        })
        summary = restore.apply_sync(blob)
        assert summary["safety_backup"].endswith(".tar.gz")
        assert (backup.backup_dir() / summary["safety_backup"]).exists()
