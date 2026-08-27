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


class TestPaymentSource:
    """Основание платежа: случаев ТРИ, а веток исторически писали две.

    «Счёта нет» ещё не значит «есть реквизиты» — с 26.08.2026 заявка может
    прийти без того и без другого. Пока каждое место решало это само,
    подтверждение автору обещало «оплату по указанным реквизитам», которых
    он не указывал. Признак теперь один на всех.
    """

    def test_three_cases(self):
        from tests.conftest import make_request

        assert make_request(has_invoice=True).payment_source == "invoice"
        assert make_request(has_invoice=False,
                            requisites="ИНН 7707083893").payment_source == "requisites"
        assert make_request(has_invoice=False, requisites="").payment_source == "none"

    def test_no_text_promises_requisites_that_do_not_exist(self):
        """Ни один текст не должен обещать реквизиты у пустой заявки."""
        from services.intake import _SOURCE_MARKS, _SOURCE_WORDS
        from services.notifier import _format_card
        from services.pdf_report import build_request_pdf
        from tests.conftest import make_request

        empty = make_request(has_invoice=False, requisites="")
        # Ловим не слово «реквизиты» — оно законно в отрицании, — а ОБЕЩАНИЕ
        # платить по ним: «по реквизитам», «по указанным реквизитам».
        promise = ("по реквизитам", "по указанным реквизитам")

        card = _format_card(empty, row_number=1)
        assert "Ни счёта, ни реквизитов" in card
        assert not any(x in card.lower() for x in promise), card

        for text in (_SOURCE_WORDS["none"], _SOURCE_MARKS["none"]):
            assert not any(x in text.lower() for x in promise), text
        # А в своих случаях обещание как раз должно быть — иначе проверка
        # прошла бы и на тексте, из которого реквизиты вырезали совсем.
        assert "по реквизитам" in _SOURCE_MARKS["requisites"]

        pdf = build_request_pdf(empty)
        assert pdf[:4] == b"%PDF", "PDF пустой заявки вообще не собрался"
