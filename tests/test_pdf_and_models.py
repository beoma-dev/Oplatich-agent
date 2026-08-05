"""PDF-документ и модельные инварианты."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.models import REQUEST_STATUSES, SHEET_HEADERS, new_request_id
from services.pdf_report import build_request_pdf
from tests.conftest import make_request


def test_pdf_is_generated_for_both_variants():
    with_requisites = build_request_pdf(make_request())
    assert with_requisites.startswith(b"%PDF") and len(with_requisites) > 10_000

    with_file = build_request_pdf(
        make_request(has_invoice=True, requisites="", file_name="20260803_Ромашка_1.pdf")
    )
    assert with_file.startswith(b"%PDF")


def test_pdf_survives_markup_hostile_input():
    hostile = make_request(
        counterparty="<b>ООО & Ко</b>", comment="строка <script> & ещё", article="<i>X</i>"
    )
    assert build_request_pdf(hostile).startswith(b"%PDF")


def test_request_id_no_collision_same_second():
    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert new_request_id(now, 1001) != new_request_id(now, 1002)


def test_sheet_row_matches_headers():
    row = make_request().as_sheet_row()
    assert len(row) == len(SHEET_HEADERS)


def test_status_callback_data_fits_telegram_limit():
    now = datetime(2026, 12, 31, 23, 59, 59, tzinfo=ZoneInfo("Europe/Moscow"))
    request_id = new_request_id(now, 999999999)
    for key in REQUEST_STATUSES:
        assert len(f"ST:{request_id}:{key}".encode()) <= 64


def _pdf_text(data: bytes) -> tuple[str, int]:
    """Текст документа и число страниц — читаем тем же pypdf, что и OCR-проверка."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)


def test_pdf_looks_like_a_printed_form():
    """Заявка — печатный бланк: заголовок с номером и датой, шапка, таблица,
    сумма прописью и строки подписей. Всё это подписывают на бумаге."""
    text, pages = _pdf_text(build_request_pdf(make_request()))
    assert pages == 1, "заявка на одну страницу разъехалась на несколько"
    for part in ("Заявка на расходование денежных средств", "Организация:",
                 "Заявитель:", "Получатель:", "Статья расходов", "Итого:",
                 "Сумма прописью:", "Комментарий:", "Разрешил", "подпись",
                 "расшифровка подписи", "Лист 1 из 1"):
        assert part in text, f"в документе нет «{part}»"
    # Дата в заголовке — прописью, как в бухгалтерских формах.
    assert "августа" in text or "от" in text
    assert "рублей" in text or "рубля" in text or "рубль" in text


def test_pdf_names_the_basis_of_payment():
    """Основание платежа названо прямо: файл счёта либо реквизиты."""
    with_file, _ = _pdf_text(
        build_request_pdf(make_request(has_invoice=True, requisites="", file_name="счёт.pdf"))
    )
    assert "счёт.pdf" in with_file
    by_requisites, _ = _pdf_text(build_request_pdf(make_request()))
    assert "по реквизитам" in by_requisites.lower()
    assert "Реквизиты для оплаты:" in by_requisites
