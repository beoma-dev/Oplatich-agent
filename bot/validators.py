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

# Сколько дополнительных документов можно приложить к одной заявке.
# Не «сколько угодно»: каждый файл — отдельная загрузка в Drive (по 2,6 с
# на боевом канале) и отдельная строка в карточке финансиста. Пять покрывает
# договор + акт + спецификацию + пару приложений.
MAX_EXTRA_FILES = 5

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


# ── Осмысленность текста ───────────────────────────────────────────────────
# Два разных инструмента, и разница между ними принципиальна.
#
# looks_broken — ЖЁСТКИЙ отказ, и потому в нём только правила, которые не
# могут ошибиться: название без единой буквы не существует, как не существует
# контрагента из одного символа. Ложное срабатывание тут стоит дорого —
# человек не оплатит счёт, — поэтому «подозрительно» сюда не попадает.
#
# looks_like_gibberish — ПРЕДУПРЕЖДЕНИЕ, его можно проигнорировать и отправить.
# Здесь эвристики, которые иногда ошибутся: доля согласных, доля цифр. Так
# ловится «лрнпдлдбншопнл», но редкое настоящее название не блокируется.
_VOWELS = set("аеёиоуыэюяaeiouy")
_LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_REPEAT_RE = re.compile(r"(.)\1{5,}", re.UNICODE)
_WORD_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def looks_broken(value: str, *, require_letter: bool = True) -> str | None:
    """Причина отказа или None. Только безошибочные правила.

    `require_letter=False` — для полей, где значение законно бывает без букв:
    срок исполнения работ пишут и датой («15.12.2026»).
    """
    text = (value or "").strip()
    if len(text) < 2:
        return "слишком короткое значение"
    if require_letter and not _LETTERS_RE.search(text):
        return "в значении нет ни одной буквы"
    if _REPEAT_RE.search(text):
        return "один символ повторяется подряд шесть раз и больше"
    return None


# Минимум букв в слове, чтобы о нём вообще судить. Короткие сокращения
# («ТД», «АО», «МГУ», «Член») под подозрение не попадают.
_JUDGE_FROM = 8


def _word_looks_random(word: str) -> bool:
    """Похоже ли ОДНО слово на набор с клавиатуры."""
    letters = _LETTERS_RE.findall(word)
    if len(letters) < _JUDGE_FROM:
        return False
    vowels = sum(1 for ch in letters if ch in _VOWELS)
    # В живом русском и английском гласных примерно треть. Ниже 15% — это
    # набор с клавиатуры: «лрнпдлдбншопнл» — одна гласная на четырнадцать.
    if vowels / len(letters) < 0.15:
        return True
    # Бедный алфавит. «выавываыавыавы» — 64% гласных, по прошлому правилу
    # не ловилось, но собрано ВСЕГО ИЗ ТРЁХ букв, которые ходят по кругу.
    # Порог абсолютный, а не долей: у длинного настоящего названия букв
    # всегда больше, а доля с ростом длины падает и у осмысленного текста.
    return len(set(letters)) <= 4


def looks_like_gibberish(value: str) -> bool:
    """Похоже на случайный набор символов. Это ПОВОД СПРОСИТЬ, а не отказать.

    Судим ПО СЛОВАМ: «Член выавываыавыавы» целиком даёт семь различных букв
    и выглядит прилично, хотя второе слово — набор из трёх букв по кругу.
    """
    text = (value or "").strip().lower()
    # Правила по ДОЛЕ цифр здесь нет намеренно. «Счёт №101 от 21.08.2026» —
    # одиннадцать цифр против шести букв, и это совершенно нормальный
    # комментарий; так же выглядят номера договоров и даты. А значение из
    # ОДНИХ цифр в названии и без того отвергает жёсткий слой: букв нет.
    return any(_word_looks_random(word) for word in _WORD_SPLIT_RE.split(text) if word)


# ── Мат ────────────────────────────────────────────────────────────────────
# Проверка НЕ блокирует отправку: она поднимает флаг, по которому админ
# получает уведомление. Запрет провоцирует обходы (латиница вперемешку,
# звёздочки) и стоит ложных срабатываний, а заявка и так неанонимна.
#
# Искать корни подстрокой нельзя: «еб» сидит в «потребность», «хлебный»,
# «требование», а «муд» — в «мудрый». Поэтому корень засчитывается только
# в начале слова или сразу после приставки — именно так строятся производные
# от мата, и обычные слова под это правило не подпадают.
_LOOKALIKE = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
    "k": "к", "m": "м", "t": "т", "b": "в", "h": "н", "u": "и", "3": "з",
    "0": "о", "4": "ч", "1": "и", "@": "а", "*": "", ".": "", "-": "", "_": "",
})
_PREFIXES = (
    "за", "вы", "до", "у", "на", "по", "от", "при", "раз", "рас", "с", "со",
    "об", "о", "пере", "под", "про", "недо", "не", "ни", "из", "в", "вз",
)
# Корни матерных производных. Полные формы там, где корень совпадает с
# обычным словом («муд» — это ещё и «мудрый»).
_PROFANITY_ROOTS = (
    "хуй", "хуе", "хуё", "хуя", "хую", "хуи",
    "пизд", "еба", "ебу", "ебо", "ебё", "ебл", "ебн", "ебт", "ёба", "ёбл",
    "бляд", "блят", "мудак", "мудил", "мудоз", "гандон", "гондон", "залуп",
    "долбоё", "долбое", "пидор", "пидар",
)
# «хер» корнем не берём: он сидит в «Херсоне» и «хересе», а это адрес и
# товар. Мягкие формы ловятся целыми словами ниже.
# Слова целиком, которые корнем не поймать.
_PROFANITY_WORDS = {"бля", "нахуй", "нахер", "похер"}


def has_profanity(value: str) -> bool:
    """Есть ли мат. Только сигнал для админа, отправку не блокирует."""
    text = (value or "").lower().translate(_LOOKALIKE)
    for word in _WORD_SPLIT_RE.split(text):
        if not word:
            continue
        if word in _PROFANITY_WORDS:
            return True
        for root in _PROFANITY_ROOTS:
            start = word.find(root)
            if start == 0 or (start > 0 and word[:start] in _PREFIXES):
                return True
    return False


def validate_line_field(
    raw: str, *, field_name: str, max_len: int = 500, required: bool = False
) -> str:
    """Однострочное поле: переводы строк сворачиваются в пробел.

    Свернуть их обязательно — значение попадает в карточку и в PDF одной
    строкой, а многострочный ввод там разъезжается. `required=False`
    разрешает пустое значение и возвращает пустую строку.
    """
    value = re.sub(r"\s+", " ", (raw or "").strip())
    if not value and not required:
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
