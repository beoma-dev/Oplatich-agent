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
    # Лист реестра пришпилен по gid; в моке он не первый и не нулевой нарочно —
    # иначе тест прошёл бы и на старом поведении «берём первый лист».
    monkeypatch.setattr(settings, "google_sheet_gid", 7)
    gb.reset_sheet_ref()
    # По умолчанию лист «уже оформлен» (закреплённая шапка) — стили не трогаем.
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Справочник"}},
            {"properties": {"sheetId": 7, "title": "Реестр",
                            "gridProperties": {"frozenRowCount": 1}}},
        ]
    }
    yield service
    gb.reset_sheet_ref()


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
    assert update_kwargs["range"] == "'Реестр'!G3"               # «Статус оплаты» найденной строки
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
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Справочник"}},
            {"properties": {"sheetId": 7, "title": "Реестр",
                            "gridProperties": {"frozenRowCount": 0}}},
        ]
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
    # Оформление адресовано листу реестра (gid 7), а не первому в книге (gid 0).
    assert freeze["updateSheetProperties"]["properties"]["sheetId"] == 7


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


def test_every_range_names_the_registry_sheet(sheets, tmp_paths):
    """Ни один диапазон не уходит безымянным.

    Безымянный диапазон Google адресует ПЕРВОМУ листу книги. Пока лист был
    один, это сходило с рук; рядом со «Справочником» перестановка вкладок
    молча уводила бы заявки в соседнюю таблицу. Тест держит инвариант для
    всех операций реестра сразу, а не для той, которую вспомнили.
    """
    r = make_request()
    sheets.get.return_value.execute.return_value = {"values": [r.as_sheet_row()]}
    sheets.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "'Реестр'!A2:O2"}
    }

    gb.append_invoice_sync(r)
    gb.recent_requests_sync(5)
    gb.recent_counterparties_sync(5)
    gb.recent_by_author_sync(r.telegram_id, limit=5)

    calls = sheets.get.call_args_list + sheets.append.call_args_list
    calls += sheets.update.call_args_list
    assert calls, "мок не зафиксировал ни одного обращения — тест бесполезен"
    for call in calls:
        assert call.kwargs["range"].startswith("'Реестр'!"), call.kwargs["range"]


def test_apostrophe_in_sheet_name_is_escaped(svc, sheets, tmp_paths):
    """Апостроф в названии листа по правилам A1 удваивается."""
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Д'Артаньян",
                                   "gridProperties": {"frozenRowCount": 1}}}]
    }
    gb.reset_sheet_ref()
    sheets.get.return_value.execute.return_value = {"values": []}
    gb.recent_counterparties_sync(5)
    assert sheets.get.call_args.kwargs["range"].startswith("'Д''Артаньян'!")


def test_missing_gid_refuses_instead_of_guessing(svc, sheets, monkeypatch, tmp_paths):
    """Нет листа с нужным gid — отказ, а не «возьмём первый».

    Тихая запись не в тот лист обнаружилась бы через месяц при сверке;
    отказ поднимает критичный алерт «Заявка не сохранилась в реестр»,
    который не выключается ни одним тумблером панели.
    """
    monkeypatch.setattr(settings, "google_sheet_gid", 12345)
    gb.reset_sheet_ref()
    with pytest.raises(RuntimeError, match="gid=12345"):
        gb.append_invoice_sync(make_request())
    sheets.append.assert_not_called()
    sheets.update.assert_not_called()
