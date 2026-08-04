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
