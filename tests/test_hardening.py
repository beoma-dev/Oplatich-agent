"""Правила дат, fail-closed доступ, справочник ФИО, анти-формулы."""
from __future__ import annotations

from datetime import date

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

