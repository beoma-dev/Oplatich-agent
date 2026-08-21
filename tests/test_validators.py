"""Валидаторы: сумма, дата, текст, файл."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bot.validators import (
    MAX_FILE_SIZE_BYTES,
    ValidationError,
    has_profanity,
    looks_broken,
    looks_like_gibberish,
    parse_amount,
    parse_planned_date,
    parse_registry_filter_date,
    validate_file,
    validate_line_field,
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


class TestLineField:
    """Срок исполнения работ: свободный текст в одну строку."""

    def test_free_text_passes(self):
        for value in ("текущий месяц", "поставка в декабре", "услуга на 6 месяцев",
                      "15.12.2026"):
            assert validate_line_field(value, field_name="Срок") == value

    def test_empty_allowed_when_not_required(self):
        assert validate_line_field("", field_name="Срок") == ""
        assert validate_line_field("   ", field_name="Срок") == ""
        assert validate_line_field(None, field_name="Срок") == ""

    def test_empty_rejected_when_required(self):
        for value in ("", "   ", None):
            with pytest.raises(ValidationError):
                validate_line_field(value, field_name="Срок", required=True)

    def test_newlines_collapse(self):
        """Поле однострочное: в карточке и PDF многострочный ввод разъезжается."""
        assert validate_line_field(
            " поставка\n  в декабре ", field_name="Срок"
        ) == "поставка в декабре"

    def test_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_line_field("я" * 201, field_name="Срок", max_len=200)


class TestLooksBroken:
    """Жёсткий слой: только правила, которые не могут ошибиться."""

    def test_digits_only_rejected(self):
        """Название контрагента без единой буквы не существует."""
        assert looks_broken("12321432132132132131253542365432565324634")

    def test_single_character_rejected(self):
        assert looks_broken(".")

    def test_repeated_character_rejected(self):
        assert looks_broken("аааааааааааа")
        assert looks_broken("111111111")

    def test_normal_names_pass(self):
        for value in ("ООО «Ромашка»", "ИП Хайрулин Владислав Ренатович",
                      "Адвокат ЗУБЕНКО АНДРЕЙ ИГОРЕВИЧ", "ФГБОУ ВО «МГУ»",
                      "АО", "Аренда"):
            assert looks_broken(value) is None, value

    def test_date_passes_where_letters_are_not_required(self):
        """Срок работ законно пишут датой — букв там нет и не должно быть."""
        assert looks_broken("15.12.2026") is not None
        assert looks_broken("15.12.2026", require_letter=False) is None


class TestLooksLikeGibberish:
    """Мягкий слой: повод спросить, а не отказать."""

    def test_keyboard_mash_caught(self):
        # Ровно то, что пришло в настоящей заявке.
        assert looks_like_gibberish("лрнпдлдбншопнл")

    def test_digits_swamping_the_text_caught(self):
        """Строка из ОДНИХ цифр ловится жёстким слоем, сюда не доходит —
        здесь случай, где цифр больше, чем букв, и их много."""
        assert looks_like_gibberish("Ромашка 12321432132132132131253542365")
        assert not looks_like_gibberish("12321432132132132131253542365432565324634")

    def test_real_values_pass(self):
        for value in ("ООО «Ромашка»", "текущий месяц", "поставка в декабре",
                      "услуга на 6 месяцев", "Аренда офиса за август",
                      "ИП Хайрулин Владислав Ренатович", "15.12.2026",
                      "Договор 1202/2-2026", "ФГБОУ ВО «МГУ»"):
            assert not looks_like_gibberish(value), value

    def test_short_abbreviations_are_not_judged(self):
        """«ТД», «АО», «МГУ» — гласных мало, но судить тут не по чему."""
        for value in ("ТД", "АО", "МГУ", "ООО"):
            assert not looks_like_gibberish(value), value


class TestProfanity:
    """Мат: сигнал админу, отправку не блокирует."""

    def test_caught(self):
        for value in ("хуйня", "заебал", "пиздец", "бля", "мудак", "нахуй",
                      "п.и.з.д.е.ц", "xyйня", "долбоёб"):
            assert has_profanity(value), value

    def test_ordinary_words_are_not_profanity(self):
        """Корни сидят внутри обычных слов — подстрокой искать нельзя."""
        for value in ("потребность", "требование", "требовать", "хлебный",
                      "гребной", "погребение", "мудрый", "мудрость", "область",
                      "сукно", "хуже", "небо", "перебои", "употребление",
                      "Херсон", "херес", "ООО «Ромашка»", "Аренда офиса"):
            assert not has_profanity(value), value

    def test_empty_is_safe(self):
        assert not has_profanity("")
        assert not has_profanity(None)
