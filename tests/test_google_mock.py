"""Google-бэкенд на моках googleapiclient: контракт вызовов.

Ловит регрессии вида «снова USER_ENTERED», «трогаем чужую шапку»,
«номер строки из имени листа».
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import services.google_backend as gb
from bot.models import SHEET_HEADERS
from config import settings
from tests.conftest import make_request


@pytest.fixture()
def svc(monkeypatch):
    service = MagicMock()
    monkeypatch.setattr(gb, "_sheets", lambda: service)
    monkeypatch.setattr(settings, "google_sheet_id", "SHEET_ID")
    # По умолчанию лист «уже оформлен» (закреплённая шапка) — стили не трогаем.
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "gridProperties": {"frozenRowCount": 1}}}]
    }
    return service


@pytest.fixture()
def sheets(svc):
    return svc.spreadsheets.return_value.values.return_value


def test_append_is_raw_overwrite_and_keeps_foreign_header(sheets, tmp_paths):
    sheets.get.return_value.execute.return_value = {"values": [SHEET_HEADERS[:9]]}
    sheets.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "SHEET1!A5:N5"}
    }
    row = gb.append_invoice_sync(make_request())

    kwargs = sheets.append.call_args.kwargs
    assert kwargs["valueInputOption"] == "RAW"          # инъекция формул закрыта
    assert kwargs["insertDataOption"] == "OVERWRITE"    # не копируем оформление шапки
    sheets.update.assert_not_called()                   # чужая шапка неприкосновенна
    # Имя листа «SHEET1» не спутано с ячейкой: A5 → строка 5 → запись №4.
    assert row == 4


def test_empty_sheet_gets_our_headers(sheets, tmp_paths):
    sheets.get.return_value.execute.return_value = {}
    sheets.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Лист1!A2:N2"}
    }
    assert gb.append_invoice_sync(make_request()) == 1
    header_body = sheets.update.call_args.kwargs["body"]
    assert header_body == {"values": [SHEET_HEADERS]}


def test_set_status_updates_status_cell_by_request_id(sheets, tmp_paths):
    r = make_request()
    full_row = r.as_sheet_row()
    sheets.get.return_value.execute.side_effect = [
        {"values": [["INV-другой"], [r.request_id]]},   # колонка ID: совпадение во 2-й записи
        {"values": [full_row]},                          # чтение всей строки после апдейта
    ]
    row = gb.set_status_sync(r.request_id, "Оплачена", )

    update_kwargs = sheets.update.call_args.kwargs
    assert update_kwargs["range"] == "G3"               # «Статус оплаты» найденной строки
    assert update_kwargs["valueInputOption"] == "RAW"
    assert update_kwargs["body"] == {"values": [["Оплачена"]]}
    assert row is not None and row["Контрагент"] == r.counterparty


def test_set_status_missing_returns_none(sheets, tmp_paths):
    sheets.get.return_value.execute.side_effect = [{"values": [["INV-нет"]]}]
    assert gb.set_status_sync("INV-00000000-000000-0000", "Оплачена") is None
    sheets.update.assert_not_called()


def test_styling_applied_once_by_frozen_marker(svc, sheets, tmp_paths):
    """Неоформленный лист (frozenRowCount=0) получает стили одним batchUpdate."""
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "gridProperties": {"frozenRowCount": 0}}}]
    }
    sheets.get.return_value.execute.return_value = {"values": [SHEET_HEADERS[:9]]}
    sheets.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Лист1!A2:N2"}
    }
    gb.append_invoice_sync(make_request())

    batch = svc.spreadsheets.return_value.batchUpdate
    assert batch.called
    requests = batch.call_args.kwargs["body"]["requests"]
    kinds = [next(iter(r)) for r in requests]
    assert "setBasicFilter" in kinds
    assert kinds.count("addConditionalFormatRule") == 5  # 4 статуса + «Срочно»
    freeze = next(r for r in requests if "updateSheetProperties" in r)
    assert freeze["updateSheetProperties"]["properties"]["gridProperties"]["frozenRowCount"] == 1


def test_amount_written_as_number(sheets, tmp_paths):
    sheets.get.return_value.execute.return_value = {"values": [SHEET_HEADERS[:9]]}
    sheets.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Лист1!A2:N2"}
    }
    gb.append_invoice_sync(make_request())
    row = sheets.append.call_args.kwargs["body"]["values"][0]
    assert isinstance(row[gb._AMOUNT_IDX], float)  # сортировка/автосумма в таблице


def test_upload_inherits_folder_permissions(monkeypatch, tmp_paths):
    drive = MagicMock()
    monkeypatch.setattr(gb, "_drive", lambda: drive)
    monkeypatch.setattr(settings, "google_drive_folder_id", "FOLDER")
    drive.files.return_value.create.return_value.execute.return_value = {
        "id": "f1",
        "webViewLink": "https://drive.google.com/file/d/f1",
    }
    link = gb.upload_invoice_file_sync(b"%PDF", "bill.pdf")

    assert link.endswith("/f1")
    kwargs = drive.files.return_value.create.call_args.kwargs
    assert kwargs["body"]["parents"] == ["FOLDER"]
    assert kwargs["supportsAllDrives"] is True
    drive.permissions.assert_not_called()  # публичные ссылки не создаются


def test_recent_by_author_filters_and_reverses(sheets, tmp_paths):
    """«Мои заявки» в Google-режиме: только свои строки, новые сверху."""
    mine1 = make_request(telegram_id=100, counterparty="Первый").as_sheet_row()
    alien = make_request(telegram_id=200, counterparty="Чужой").as_sheet_row()
    mine2 = make_request(telegram_id=100, counterparty="Второй").as_sheet_row()
    sheets.get.return_value.execute.return_value = {"values": [mine1, alien, mine2]}

    rows = gb.recent_by_author_sync(100, limit=10)
    assert [r["Контрагент"] for r in rows] == ["Второй", "Первый"]


def test_counterparty_book_takes_last_requisites(sheets, tmp_paths):
    no_req = make_request(counterparty="ООО «Ромашка»", requisites="").as_sheet_row()
    with_req = make_request(
        counterparty="ООО «Ромашка»", requisites="ИНН 7707083893"
    ).as_sheet_row()
    other = make_request(counterparty="ИП Петров").as_sheet_row()
    sheets.get.return_value.execute.return_value = {"values": [no_req, with_req, other]}

    book = gb.counterparty_book_sync(6)
    assert book[0] == {"name": "ООО «Ромашка»", "requisites": "ИНН 7707083893"}


def test_short_rows_are_padded(sheets, tmp_paths):
    """Google обрезает строку по последней заполненной ячейке — дополняем."""
    sheets.get.return_value.execute.return_value = {"values": [["2026-08-04 10:00"]]}
    assert gb.get_request_sync("INV-20260804-100000-0001") is None
    assert gb.counterparty_book_sync(6) == []


def test_recent_requests_returns_all_authors_newest_first(sheets, tmp_paths):
    """Панель финансиста в Google-режиме видит заявки всех сотрудников."""
    first = make_request(telegram_id=100, counterparty="Первый").as_sheet_row()
    second = make_request(telegram_id=200, counterparty="Второй").as_sheet_row()
    sheets.get.return_value.execute.return_value = {"values": [first, second]}

    rows = gb.recent_requests_sync(limit=10)
    assert [r["Контрагент"] for r in rows] == ["Второй", "Первый"]
