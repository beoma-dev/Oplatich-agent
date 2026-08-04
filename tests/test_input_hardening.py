"""Устойчивость к некорректному вводу в конфигурации и админ-операциях."""
from __future__ import annotations

from config import _parse_id_list
from services.runtime_settings import valid_financier_entry


class TestParseIdList:
    def test_valid(self):
        assert _parse_id_list("111, 222,333") == [111, 222, 333]

    def test_garbage_skipped_not_crashing(self):
        # «@username» и опечатки в ADMIN_IDS/ALLOWED_USER_IDS не должны
        # ронять приложение — пропускаются с warning.
        assert _parse_id_list("111, @admin, abc, 222, 12x") == [111, 222]

    def test_empty(self):
        assert _parse_id_list("") == []
        assert _parse_id_list(None) == []
        assert _parse_id_list(" , ,") == []


class TestFinancierEntry:
    def test_numeric_ok(self):
        assert valid_financier_entry("969015071")
        assert valid_financier_entry("-1001234567890")

    def test_username_ok(self):
        assert valid_financier_entry("@valid_name")
        assert valid_financier_entry("valid_name")

    def test_invalid_rejected(self):
        assert not valid_financier_entry("")
        assert not valid_financier_entry("@")
        assert not valid_financier_entry("abc")            # короче 5
        assert not valid_financier_entry("имя_кириллицей")
        assert not valid_financier_entry("name with space")
        assert not valid_financier_entry("<script>alert</script>")
        assert not valid_financier_entry("x" * 33)
