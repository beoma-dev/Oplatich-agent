"""Автопроверка вложения: похоже ли оно на счёт (а не на картинку с котиком).

Две ступени:
  1) PDF с текстовым слоем — извлечение текста (pypdf);
  2) картинки и PDF-сканы — OCR Tesseract rus+eng (для сканов страницы
     рендерятся через pdf2image/poppler).

Скоринг по маркерам платёжного документа (настроен по реальному счёту):
заголовок «Счет на оплату №», валидный ИНН (контрольные числа), БИК и
банковские счета, «Итого/Всего к оплате», НДС, срок оплаты, сумма прописью
и — сильный сигнал — совпадение суммы из формы с суммой в документе.

Реакция МЯГКАЯ: при низком скоринге заявка НЕ блокируется — автор и
финансист получают предупреждение, событие уходит в аудит. Любой сбой
проверки (нет tesseract, битый файл) = fail-open: подача не страдает.
"""
from __future__ import annotations

import io
import logging
import re
from decimal import Decimal

from config import settings

log = logging.getLogger(__name__)

# Порог «похоже на счёт». Реальный счёт набирает 10+, котик — 0–2.
THRESHOLD = 5

WARNING_TEXT = (
    "⚠️ Автопроверка: вложение не похоже на счёт "
    "(маркеры платёжного документа не найдены). Проверьте файл."
)

_MARKERS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"сч[её]т\s*(на\s*оплату|№|N\b)", re.I), 3, "заголовок счёта"),
    (re.compile(r"итого|всего\s+к\s+оплате", re.I), 2, "итог"),
    (re.compile(r"поставщик|исполнитель", re.I), 1, "поставщик"),
    (re.compile(r"покупатель|заказчик|плательщик", re.I), 1, "покупатель"),
    (re.compile(r"\bНДС\b", re.I), 1, "НДС"),
    (re.compile(r"БИК\s*\d{9}", re.I), 1, "БИК"),
    (re.compile(r"\b(?:407|408|301)\d{17}\b"), 2, "банковский счёт"),
    (re.compile(r"оплатить\s+не\s+позднее|срок\s+оплаты", re.I), 1, "срок оплаты"),
    (re.compile(r"руб(?:л[ьяе]й?|\.)|копе[ейка]{1,3}", re.I), 1, "сумма прописью"),
]


def _inn_valid(inn: str) -> bool:
    """Контрольные числа ИНН (10 и 12 знаков) — зеркало webapp/form-lib.js."""
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        return False
    d = [int(c) for c in inn]

    def ctrl(weights: list[int]) -> int:
        return sum(w * d[i] for i, w in enumerate(weights)) % 11 % 10

    if len(d) == 10:
        return ctrl([2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[9]
    return (
        ctrl([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[10]
        and ctrl([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[11]
    )


def _amount_variants(amount: Decimal) -> set[str]:
    """«174387.21», «174387,21», «174 387,21» (+ целые без копеек)."""
    s = f"{amount:.2f}"
    int_part, frac = s.split(".")
    spaced = f"{int(int_part):,}".replace(",", " ")
    variants = {
        f"{int_part}.{frac}", f"{int_part},{frac}",
        f"{spaced},{frac}", f"{spaced}.{frac}",
    }
    if frac == "00":
        variants |= {int_part, spaced}
    return variants


def score_text(text: str, expected_amount: Decimal | None = None) -> tuple[int, list[str]]:
    """Скоринг «похожести на счёт». Возвращает (баллы, найденные маркеры)."""
    score = 0
    found: list[str] = []
    for rx, weight, name in _MARKERS:
        if rx.search(text):
            score += weight
            found.append(name)

    inns = re.findall(r"ИНН\D{0,5}(\d{12}|\d{10})", text)
    if any(_inn_valid(i) for i in inns):
        score += 3
        found.append("валидный ИНН")

    if expected_amount is not None:
        normalized = text.replace(" ", " ")
        if any(v in normalized for v in _amount_variants(expected_amount)):
            score += 3
            found.append("сумма совпадает с формой")

    return score, found


# ---------------------------------------------------------------------------
# Извлечение текста
# ---------------------------------------------------------------------------
def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:3])


def _ocr_image_bytes(content: bytes) -> str:
    import pytesseract
    from PIL import Image

    with Image.open(io.BytesIO(content)) as img:
        return pytesseract.image_to_string(img, lang="rus+eng")


def _ocr_pdf(content: bytes) -> str:
    import pytesseract
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(content, dpi=200, first_page=1, last_page=2)
    return "\n".join(pytesseract.image_to_string(p, lang="rus+eng") for p in pages)


def _extract_text(content: bytes, filename: str) -> tuple[str, bool]:
    """(текст, удалось_ли_извлечь). False = fail-open, не пугаем пользователя."""
    is_pdf = content[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    if is_pdf:
        try:
            text = _pdf_text(content)
        except Exception:  # noqa: BLE001 — битый/нестандартный PDF
            text = ""
        if len(text.strip()) >= 80:
            return text, True
        # Текстового слоя нет — вероятно скан: пробуем OCR.
        try:
            return _ocr_pdf(content), True
        except Exception:  # noqa: BLE001 — нет poppler/tesseract и т.п.
            log.warning("OCR PDF недоступен — автопроверка счёта пропущена")
            return "", False
    try:
        return _ocr_image_bytes(content), True
    except Exception:  # noqa: BLE001 — нет tesseract / не изображение
        log.warning("OCR изображения недоступен — автопроверка счёта пропущена")
        return "", False


def inspect_invoice_file(
    content: bytes, filename: str, expected_amount: Decimal | None = None
) -> tuple[str | None, str]:
    """(предупреждение или None, распознанный текст).

    Текст отдаётся наружу, чтобы автозаполнение формы (services/invoice_extract)
    разбирало уже готовый результат, а не гоняло OCR второй раз: распознавание
    скана — самая дорогая операция во всём сценарии.

    Синхронная и потенциально тяжёлая (OCR) — вызывать через to_thread.
    """
    if not settings.invoice_check_enabled:
        return None, ""
    try:
        text, extracted = _extract_text(content, filename)
        if not extracted:
            return None, ""  # fail-open: наши проблемы — не проблемы сотрудника
        score, found = score_text(text, expected_amount)
        if score >= THRESHOLD:
            log.info("Файл «%s» похож на счёт: %s (баллы %s)", filename, found, score)
            return None, text
        log.warning(
            "Файл «%s» НЕ похож на счёт: баллы %s, найдено %s", filename, score, found
        )
        return WARNING_TEXT, text
    except Exception:  # noqa: BLE001 — проверка не должна ломать подачу
        log.exception("Сбой автопроверки счёта — пропущена")
        return None, ""


def check_invoice_file(
    content: bytes, filename: str, expected_amount: Decimal | None = None
) -> str | None:
    """None — похоже на счёт (или проверить не удалось); иначе предупреждение."""
    return inspect_invoice_file(content, filename, expected_amount)[0]
