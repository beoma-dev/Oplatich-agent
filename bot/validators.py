"""Валидация и нормализация пользовательского ввода."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

# Разрешённые MIME-типы файла счёта.
ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.ms-excel",  # xls
}

# Максимальный размер файла — 20 МБ (лимит Telegram Bot API на скачивание).
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024

_AMOUNT_RE = re.compile(r"[^\d,.\-]")
# Корректная группировка тысяч: 1.000, 12.345.678 / 1,000, 12,345,678.
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")
_THOUSANDS_COMMA = re.compile(r"^\d{1,3}(,\d{3})+$")


class ValidationError(ValueError):
    """Ошибка валидации ввода — текст пойдёт пользователю."""


def parse_amount(raw: str) -> Decimal:
    """Парсит сумму из строки: "125 000,50", "125000.5", "1.000.000,50".

    Поддерживает оба стиля разделителей: если есть и точка, и запятая —
    десятичным считается ПОСЛЕДНИЙ разделитель («1.000,50» → 1000.50,
    «1,000.50» → 1000.50); несколько одинаковых — разделители тысяч.
    Возвращает Decimal с двумя знаками. Бросает ValidationError на мусоре.
    """
    cleaned = _AMOUNT_RE.sub("", raw).replace(" ", "")
    has_dot, has_comma = "." in cleaned, "," in cleaned

    def _bad() -> ValidationError:
        return ValidationError(
            "Не удалось распознать сумму. Введите число, например: 125000 или 1 000 000,50"
        )

    if has_dot and has_comma:
        dec = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thou_re = _THOUSANDS_DOT if dec == "," else _THOUSANDS_COMMA
        int_part, _, frac = cleaned.rpartition(dec)
        # Целая часть обязана быть корректной группировкой тысяч,
        # иначе «1.00,50» молча превратился бы в неверную сумму.
        if not thou_re.fullmatch(int_part):
            raise _bad()
        cleaned = int_part.replace(".", "").replace(",", "") + "." + frac
    elif has_comma:
        # Одна запятая — десятичная; несколько — только корректные тысячи.
        if cleaned.count(",") == 1:
            cleaned = cleaned.replace(",", ".")
        elif _THOUSANDS_COMMA.fullmatch(cleaned):
            cleaned = cleaned.replace(",", "")
        else:
            raise _bad()
    elif cleaned.count(".") > 1:
        if _THOUSANDS_DOT.fullmatch(cleaned):
            cleaned = cleaned.replace(".", "")
        else:
            raise _bad()  # «12..5» — мусор, а не 125

    if not cleaned:
        raise ValidationError("Не удалось распознать сумму. Введите число, например: 125000")

    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValidationError("Сумма должна быть числом, например: 125000 или 125000.50") from exc

    if amount <= 0:
        raise ValidationError("Сумма должна быть больше нуля.")
    if amount > Decimal("1_000_000_000"):
        raise ValidationError("Сумма слишком большая — проверьте ввод.")

    return amount.quantize(Decimal("0.01"))


_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


def parse_planned_date(raw: str, *, today: date | None = None) -> date:
    """Парсит плановую дату оплаты: «15.08.2026», «2026-08-15» и т.п.

    Не в прошлом и не дальше двух лет вперёд. Бросает ValidationError.
    """
    value = (raw or "").strip()
    if not value:
        raise ValidationError("Укажите плановую дату оплаты.")

    parsed: date | None = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValidationError(
            "Не удалось распознать дату. Введите в формате ДД.ММ.ГГГГ, например 15.08.2026."
        )

    today = today or date.today()
    if parsed < today:
        raise ValidationError("Плановая дата оплаты не может быть в прошлом.")
    if parsed > today + timedelta(days=730):
        raise ValidationError("Дата больше чем на два года вперёд — проверьте год.")
    return parsed


def parse_registry_filter_date(raw: str) -> date | None:
    """Дата для фильтров панели финансиста; пусто → None.

    В отличие от плановой даты, прошлое разрешено: финансист смотрит и
    историю платежей. Форматы — те же, что принимает форма.
    """
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValidationError(
        "Не удалось распознать дату фильтра. Формат: ДД.ММ.ГГГГ, например 15.08.2026."
    )


def validate_text_field(raw: str, *, field_name: str, max_len: int = 500) -> str:
    """Обрезает пробелы, проверяет непустоту и длину."""
    value = (raw or "").strip()
    if not value:
        raise ValidationError(f"Поле «{field_name}» не может быть пустым.")
    if len(value) > max_len:
        raise ValidationError(f"Поле «{field_name}» слишком длинное (макс. {max_len} символов).")
    return value


def validate_optional_text_field(raw: str, *, field_name: str, max_len: int = 500) -> str:
    """То же, что validate_text_field, но пустое значение допустимо.

    Переводы строк сворачиваем в пробел: поле однострочное, а в карточку и
    в PDF оно попадает в одну строку — многострочный ввод там разъезжается.
    """
    value = re.sub(r"\s+", " ", (raw or "").strip())
    if not value:
        return ""
    return validate_text_field(value, field_name=field_name, max_len=max_len)


def validate_file(mime_type: str | None, file_size: int | None) -> None:
    """Проверяет тип и размер прикреплённого файла счёта."""
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            "Неподдерживаемый формат файла. Пришлите счёт в PDF, JPG, PNG или XLSX."
        )
    if file_size is not None and file_size > MAX_FILE_SIZE_BYTES:
        raise ValidationError("Файл больше 20 МБ — Telegram не позволит его скачать.")
