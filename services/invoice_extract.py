"""Разбор счёта на поля формы (бета).

Текст счёта уже извлекается автопроверкой (`invoice_check`) — PDF-слой или
OCR. Здесь он разбирается на конкретные значения: сумма, контрагент, ИНН,
банковские реквизиты, номер и дата счёта.

Три правила, из которых всё вытекает:

  • НИЧЕГО не угадываем наполовину. Поле возвращается, только если найдено
    уверенно (ИНН — с проверкой контрольных чисел, счета — по маске и длине).
    Пустое поле честнее подставленного мусора: мусор человек не заметит.
  • Ошибка разбора не влияет ни на подачу, ни на автопроверку: любой сбой —
    пустой результат.
  • Решение всегда за человеком. Модуль ничего не заполняет, он только
    предлагает; подстановку делает пользователь нажатием в форме.

Текст после OCR грязный: съезжают пробелы, путаются похожие символы,
колонки склеиваются. Поэтому каждое поле ищется независимо — одно
нераспознанное не роняет остальные.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from services.invoice_check import _inn_valid

log = logging.getLogger(__name__)

# Неразрывный и узкий пробелы: в счетах ими разделяют тысячи.
_SPACES = "   "


def _clean(text: str) -> str:
    for ch in _SPACES:
        text = text.replace(ch, " ")
    return text


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


# ── Сумма ──────────────────────────────────────────────────────────────────
# «Итого к оплате: 174 387,21» / «Всего к оплате 174387.21».
_TOTAL_RE = re.compile(
    r"(?:итого|всего)\s*(?:к\s*оплате)?\s*[:\-]?\s*([\d][\d\s.,]{2,20})",
    re.I,
)


def _parse_money(raw: str) -> Decimal | None:
    """«174 387,21» → Decimal. Мусор и нули отбрасываем."""
    cleaned = re.sub(r"[^\d.,]", "", raw or "")
    if not cleaned:
        return None
    # Последний разделитель считаем десятичным, если за ним ровно две цифры.
    m = re.search(r"[.,](\d{2})$", cleaned)
    if m:
        head = re.sub(r"[^\d]", "", cleaned[: m.start()])
        cleaned = f"{head}.{m.group(1)}"
    else:
        cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0 or value > Decimal("1_000_000_000"):
        return None
    return value.quantize(Decimal("0.01"))


def find_amount(text: str) -> Decimal | None:
    """Сумма к оплате: берём НАИБОЛЬШЕЕ из «итого», это и есть итог."""
    candidates = []
    for raw in _TOTAL_RE.findall(text):
        value = _parse_money(raw)
        if value is not None:
            candidates.append(value)
    return max(candidates) if candidates else None


# ── Реквизиты ──────────────────────────────────────────────────────────────
_INN_RE = re.compile(r"ИНН\D{0,6}(\d{10}|\d{12})", re.I)
_KPP_RE = re.compile(r"КПП\D{0,6}(\d{9})", re.I)
_BIK_RE = re.compile(r"БИК\D{0,6}(\d{9})", re.I)
# Расчётный счёт: 40702…, 40802… — 20 цифр, возможно с пробелами после OCR.
# [^\S\n] — пробелы БЕЗ перевода строки: иначе счёт склеивался со следующей
# строкой и получалось «число» длиннее двадцати цифр.
# Допуск с запасом на пробелы между группами цифр — точную длину
# всё равно проверяет _first по числу цифр (ровно 20).
_ACCOUNT_RE = re.compile(r"\b((?:40[178]|30[12])[\d\t ]{16,26})")
_CORR_RE = re.compile(r"\b(301\d{2}[\d\t ]{13,24})")


def find_inn(text: str) -> str | None:
    """Первый ИНН, прошедший контрольные числа.

    В счёте их обычно два — поставщика и покупателя. Поставщик стоит выше,
    поэтому первый валидный почти всегда его. Невалидные (склеенные OCR)
    отбрасываем молча.
    """
    for candidate in _INN_RE.findall(text):
        if _inn_valid(candidate):
            return candidate
    return None


def _first(pattern: re.Pattern, text: str, length: int) -> str | None:
    for raw in pattern.findall(text):
        value = _digits(raw)
        if len(value) == length:
            return value
    return None


# ── Получатель ─────────────────────────────────────────────────────────────
# «Поставщик (Исполнитель): ООО «Ромашка»» — берём то, что после метки.
_SUPPLIER_RE = re.compile(
    r"(?:поставщик|исполнитель|получатель|продавец)"
    r"[^:\n]{0,30}:?\s*([^\n]{3,120})",
    re.I,
)

# Организационно-правовые формы. Список нарочно РАЗНЫЙ для двух путей поиска,
# и это главное здесь.
#
# _FORMS_WIDE — широкий, но применяется ТОЛЬКО с привязкой к началу строки
# получателя: в счёте форма всегда стоит перед названием. Без привязки
# короткие формы цепляют случайные слова — «ПО» находит «по договору»,
# «ГК» находит «ГК РФ ст. 421», и в поле уезжает мусор вместо контрагента.
_FORMS_WIDE = (
    # хозяйственные общества и товарищества
    "ООО", "АО", "ПАО", "НАО", "ЗАО", "ОАО", "ОДО", "ПТ", "КТ",
    # предприниматель и фермерское хозяйство
    "ИП", "КФХ",
    # унитарные, казённые, бюджетные, автономные
    "ФГУП", "ГУП", "МУП", "ФКУ", "ГКУ", "МКУ",
    "ФГБУ", "ФГАУ", "ГБУ", "ГАУ", "МБУ", "МАУ",
    # образование
    "ФГБОУ", "ФГАОУ", "ГБПОУ", "ГБОУ", "ГАОУ", "МБОУ", "МАОУ", "МБДОУ",
    "ЧОУ", "НОУ", "АНОО",
    # некоммерческие
    "НКО", "АНО", "БФ", "НФ", "НП", "РОО", "СРО",
    # кооперативы, собственники, садоводы
    "ТСЖ", "ТСН", "ЖСК", "ГСК", "ЖНК", "КПК", "СПК", "ПК",
    "СНТ", "ДНП", "ОНТ",
    # встречаются приставкой к названию
    "УК", "ТД", "НИИ",
)
# _FORMS_SAFE — узкий: только те формы, которые невозможно спутать со словом
# или сокращением из перечня товаров. Он один ходит по тексту без привязки.
_FORMS_SAFE = (
    "ООО", "АО", "ПАО", "НАО", "ЗАО", "ОАО", "ИП", "КФХ",
    "ФГУП", "ГУП", "МУП", "ФГБУ", "ФГАУ", "ГБУ", "МБУ",
    "ФГБОУ", "ФГАОУ", "ГБПОУ", "ГБОУ", "МБОУ", "МАОУ",
    "НКО", "АНО", "ТСЖ", "ТСН", "СНТ", "ЖСК", "ГСК",
)
# Та же форма словом: «Индивидуальный предприниматель Иванов И.И.». Тоже
# только с привязкой к началу — иначе «общество» из текста договора ловится.
_FORMS_SPELLED = (
    r"Индивидуальн\w+\s+предпринимател\w+",
    r"Самозанят\w+",
    r"(?:Публичное\s+|Непубличное\s+|Закрытое\s+|Открытое\s+)?"
    r"Акционерное\s+общество",
    r"Общество\s+с\s+ограниченной\s+ответственностью",
    r"Крестьянское\s*\(?фермерское\)?\s+хозяйство",
)


def _forms_alt(forms: tuple[str, ...]) -> str:
    """Длинные формы раньше коротких: «ФГБОУ» не должен совпасть как «ГБОУ»."""
    return "|".join(sorted(forms, key=len, reverse=True))


# Название после формы. Два вида, и порядок важен — сначала пробуем вариант
# с кавычками, иначе «ФГБОУ ВО «МГУ»» обрывается на «ФГБОУ ВО»: между формой
# и кавычками помещается уточнение, и его надо перескочить.
_NAME_TAIL = (
    r"\s*(?:[^\n,«»\"']{0,40}[«\"'][^\n«»\"']{2,80}[»\"']"   # … «Название»
    r"|[^\n,«»\"']{2,80})"                                      # Название без кавычек
)

# \b обязателен: без него «ип» внутри слова («Типовой») считалось формой,
# и в поле летел обрывок с середины строки.
_ORG_START_RE = re.compile(
    rf"^\W*((?:{_forms_alt(_FORMS_WIDE)}|{'|'.join(_FORMS_SPELLED)})\b{_NAME_TAIL})",
    re.I,
)
_ORG_ANY_RE = re.compile(rf"(\b(?:{_forms_alt(_FORMS_SAFE)})\b{_NAME_TAIL})", re.I)

# ФИО получателя-физлица: «Иванов Иван Иванович», «Иванов И. И.», двойные
# фамилии. Без этого счёт от ИП или самозанятого не давал НИЧЕГО, и дальше
# срабатывал запасной путь, подставляя банк.
_FIO_START_RE = re.compile(
    r"^\W*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
    r"\s+(?:[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+|[А-ЯЁ]\.\s*[А-ЯЁ]\.))"
)

# Строки банковского блока — это НЕ контрагент. В типовом счёте «Банк
# получателя: ПАО СБЕРБАНК» стоит ВЫШЕ строки получателя, поэтому «первая
# форма в тексте» почти всегда была банком — систематически, а не случайно.
_BANK_LINE_RE = re.compile(r"банк|бик|к/с|корр", re.I)


def find_counterparty(text: str) -> str | None:
    """Название получателя платежа.

    Сначала строка после «Поставщик/Получатель»: в ней форма или ФИО ищутся
    с привязкой к началу, и только потом — узким списком в любом месте.
    Если метки нет вовсе (OCR теряет двоеточия), идём по строкам текста,
    пропуская банковский блок. Фильтр банка стоит ТОЛЬКО в запасном пути:
    когда контрагент и есть банк, метка «Поставщик: АО «Альфа-Банк»»
    отрабатывает как обычно.
    """
    for raw in _SUPPLIER_RE.findall(text):
        for pattern in (_ORG_START_RE, _FIO_START_RE, _ORG_ANY_RE):
            m = pattern.search(raw)
            if m:
                return _tidy_org(m.group(1))
    for line in text.splitlines():
        if _BANK_LINE_RE.search(line):
            continue
        m = _ORG_ANY_RE.search(line)
        if m:
            return _tidy_org(m.group(1))
    return None


def _tidy_org(value: str) -> str:
    value = _clean(value).strip(" ,;")
    value = re.sub(r"\s{2,}", " ", value)
    # Хвосты вида «ИНН 7707…», приклеенные OCR к названию.
    value = re.split(r"\s+(?:ИНН|КПП|тел\.?)\b", value, flags=re.I)[0]
    value = value.strip(" ,;")
    # Точку срезаем только на конце фразы, но не у инициала: «Сидоров П. И.»
    # без неё выглядит обрубком.
    value = re.sub(r"(?<![А-ЯЁA-Z])\.+$", "", value)
    # Кавычки-ёлочки НЕ срезаем: «Ромашка» — часть названия.
    return value.strip(" ,;") or value.strip()


# ── Номер и дата счёта ─────────────────────────────────────────────────────
_NUMBER_RE = re.compile(
    r"[СсCc]ч[её]т\s*(?:на\s*оплату\s*)?№?\s*([A-Za-zА-Яа-я0-9\-/]{1,20})"
    r"\s*от\s*(\d{1,2}[.\-/\s]\w{2,9}[.\-/\s]\d{2,4})",
    re.I,
)


def find_invoice_number(text: str) -> tuple[str | None, str | None]:
    """(номер, дата) счёта — пригодится и для поиска настоящих дублей."""
    m = _NUMBER_RE.search(text)
    if not m:
        return None, None
    number = m.group(1).strip(" .,;№")
    date = re.sub(r"\s+", " ", m.group(2)).strip()
    return (number or None), (date or None)


def build_requisites(fields: dict) -> str:
    """Собирает найденное в блок реквизитов — как его пишут в платёжке."""
    lines = []
    if fields.get("counterparty"):
        lines.append(f"Получатель: {fields['counterparty']}")
    inn_kpp = []
    if fields.get("inn"):
        inn_kpp.append(f"ИНН {fields['inn']}")
    if fields.get("kpp"):
        inn_kpp.append(f"КПП {fields['kpp']}")
    if inn_kpp:
        lines.append(", ".join(inn_kpp))
    if fields.get("account"):
        lines.append(f"Р/с {fields['account']}")
    if fields.get("bik"):
        lines.append(f"БИК {fields['bik']}")
    if fields.get("corr_account"):
        lines.append(f"К/с {fields['corr_account']}")
    return "\n".join(lines)


def extract_fields(text: str) -> dict:
    """Разбирает текст счёта. Возвращает только уверенно найденное.

    Ключи: amount, counterparty, inn, kpp, bik, account, corr_account,
    invoice_number, invoice_date, requisites. Отсутствующие — не включаются.
    """
    if not text or not text.strip():
        return {}
    try:
        text = _clean(text)
        fields: dict = {}

        amount = find_amount(text)
        if amount is not None:
            fields["amount"] = f"{amount:.2f}"

        counterparty = find_counterparty(text)
        if counterparty:
            fields["counterparty"] = counterparty

        for key, pattern, length in (
            ("kpp", _KPP_RE, 9),
            ("bik", _BIK_RE, 9),
            ("account", _ACCOUNT_RE, 20),
            ("corr_account", _CORR_RE, 20),
        ):
            value = _first(pattern, text, length)
            if value:
                fields[key] = value

        inn = find_inn(text)
        if inn:
            fields["inn"] = inn

        number, date = find_invoice_number(text)
        if number:
            fields["invoice_number"] = number
        if date:
            fields["invoice_date"] = date

        requisites = build_requisites(fields)
        if requisites:
            fields["requisites"] = requisites
        return fields
    except Exception:  # noqa: BLE001 — разбор не должен ломать подачу
        log.exception("Сбой разбора счёта — автозаполнение пропущено")
        return {}
