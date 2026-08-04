"""Валидаторы: сумма, дата, текст, файл."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bot.validators import (
    MAX_FILE_SIZE_BYTES,
    ValidationError,
    parse_amount,
    parse_planned_date,
    parse_registry_filter_date,
    validate_file,
    validate_text_field,
)

TODAY = date(2026, 8, 3)


class TestParseAmount:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("125000", Decimal("125000.00")),
            ("125000.50", Decimal("125000.50")),
            ("125 000,50", Decimal("125000.50")),
            ("1 000 000 руб", Decimal("1000000.00")),
            ("123,45", Decimal("123.45")),
            ("1,234,567.89", Decimal("1234567.89")),
            # Европейский формат: точки — тысячные, запятая — десятичная.
            ("1.000,50", Decimal("1000.50")),
            ("1.000.000,50", Decimal("1000000.50")),
            ("1,000.50", Decimal("1000.50")),
            ("1.000.000", Decimal("1000000.00")),
            ("1,000,000", Decimal("1000000.00")),
        ],
    )
    def test_valid(self, raw, expected):
        assert parse_amount(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "", "ноль", "0", "-5", "1000000001", "..", "12..5",
            "1.00,50",      # битая группировка тысяч — не молчаливые 100.50
            "1000.000,50",  # неверная группировка
            "1,00,50",      # не тысячи и не десятичная
        ],
    )
    def test_invalid(self, raw):
        with pytest.raises(ValidationError):
            parse_amount(raw)


class TestParsePlannedDate:
    @pytest.mark.parametrize(
        "raw",
        ["15.08.2026", "15.08.26", "2026-08-15", "15/08/2026", "15-08-2026", " 15.08.2026 "],
    )
    def test_formats(self, raw):
        assert parse_planned_date(raw, today=TODAY) == date(2026, 8, 15)

    def test_today_allowed(self):
        assert parse_planned_date("03.08.2026", today=TODAY) == TODAY

    @pytest.mark.parametrize(
        "raw",
        [
            "",                # пусто
            "вчера",           # не дата
            "32.08.2026",      # нет такого дня
            "01.01.2020",      # прошлое
            "01.01.2030",      # дальше двух лет
            "15,08,2026",      # неподдерживаемый разделитель
        ],
    )
    def test_invalid(self, raw):
        with pytest.raises(ValidationError):
            parse_planned_date(raw, today=TODAY)


class TestTextField:
    def test_strips_and_returns(self):
        assert validate_text_field("  ООО «Ромашка»  ", field_name="Контрагент") == "ООО «Ромашка»"

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            validate_text_field("   ", field_name="Контрагент")

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            validate_text_field("x" * 501, field_name="Комментарий", max_len=500)


class TestValidateFile:
    def test_allowed(self):
        validate_file("application/pdf", 1024)  # не бросает

    def test_bad_mime(self):
        with pytest.raises(ValidationError):
            validate_file("application/x-msdownload", 1024)

    def test_too_big(self):
        with pytest.raises(ValidationError):
            validate_file("application/pdf", MAX_FILE_SIZE_BYTES + 1)


class TestRegistryFilterDate:
    """Фильтр панели финансиста: прошлое разрешено, мусор — нет."""

    def test_empty_means_no_filter(self):
        assert parse_registry_filter_date("") is None
        assert parse_registry_filter_date("   ") is None

    def test_past_date_allowed(self):
        assert parse_registry_filter_date("01.01.2020") == date(2020, 1, 1)

    def test_iso_and_ru_formats(self):
        assert parse_registry_filter_date("2026-08-15") == date(2026, 8, 15)
        assert parse_registry_filter_date("15.08.2026") == date(2026, 8, 15)

    def test_garbage_rejected(self):
        with pytest.raises(ValidationError):
            parse_registry_filter_date("позавчера")
