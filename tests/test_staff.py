"""Справочник сотрудников: разбор таблицы, хранение, подстановка ФИО."""
from __future__ import annotations

import pytest

from config import settings
from services import staff

# Ровно тот формат, что в рабочей таблице: без заголовков, ФИО и ссылка t.me.
REAL = [
    ["Хайрулин Владислав", "https://t.me/vladidas"],
    ["Микула Татьяна", "https://t.me/Tatyana_Mikula"],
    ["Елипашев Павел", "https://t.me/elementaryyy1997"],
]


class TestParse:
    def test_sheet_without_headers(self):
        """Заголовков в таблице нет — это не повод её не понять."""
        rows, notes = staff.parse(REAL)
        assert [r["full_name"] for r in rows] == [
            "Хайрулин Владислав", "Микула Татьяна", "Елипашев Павел"
        ]
        assert [r["username"] for r in rows] == [
            "vladidas", "tatyana_mikula", "elementaryyy1997"
        ]
        assert notes == []

    def test_columns_may_be_swapped(self):
        """Аккаунт узнаём по виду, а не по номеру колонки."""
        rows, _ = staff.parse([["@petya", "Петров Пётр"]])
        assert rows == [{"tg_id": None, "username": "petya",
                         "full_name": "Петров Пётр", "role": ""}]

    def test_headers_are_used_when_present(self):
        rows, _ = staff.parse([
            ["Список на 2026 год"],
            ["ФИО", "Должность", "Telegram", "ID"],
            ["Петров Пётр", "Закупки", "@petya", "12345678"],
        ])
        assert rows == [{"tg_id": 12345678, "username": "petya",
                         "full_name": "Петров Пётр", "role": "Закупки"}]

    @pytest.mark.parametrize("cell", [
        "@Petya", "petya", "t.me/petya", "https://t.me/Petya",
        "https://t.me/petya/", "https://t.me/petya?start=1",
    ])
    def test_every_way_of_writing_an_account(self, cell):
        assert staff.normalize_username(cell) == "petya"

    @pytest.mark.parametrize("cell", ["Иванов Иван", "", "—", "12", "почта@дом"])
    def test_not_an_account(self, cell):
        assert staff.normalize_username(cell) == ""

    def test_empty_rows_are_skipped_silently(self):
        rows, notes = staff.parse([["Петров Пётр", "@petya"], [], ["", ""]])
        assert len(rows) == 1 and notes == []

    def test_row_without_account_is_reported_not_dropped_silently(self):
        rows, notes = staff.parse([["Петров Пётр", ""]])
        assert rows == []
        assert "без аккаунта" in notes[0]

    def test_duplicate_account_is_reported(self):
        """Один аккаунт с двумя ФИО — какое верное, решать не нам."""
        rows, notes = staff.parse([["Петров Пётр", "@petya"], ["Пётр П.", "@petya"]])
        assert len(rows) == 1
        assert "уже встречался" in notes[0]


class TestStore:
    def test_add_and_list(self, tmp_paths):
        assert staff.add_sync("Петров Пётр", "@petya") == "added"
        assert [r["full_name"] for r in staff.all_sync()] == ["Петров Пётр"]

    def test_same_account_updates_instead_of_doubling(self, tmp_paths):
        """Человека переименовали — это правка, а не второй сотрудник."""
        staff.add_sync("Петрова Мария", "@masha")
        assert staff.add_sync("Иванова Мария", "@masha") == "updated"
        rows = staff.all_sync()
        assert len(rows) == 1 and rows[0]["full_name"] == "Иванова Мария"

    def test_name_alone_is_refused(self, tmp_paths):
        with pytest.raises(ValueError):
            staff.add_sync("Петров Пётр", "")

    def test_remove(self, tmp_paths):
        staff.add_sync("Петров Пётр", "@petya")
        row_id = staff.all_sync()[0]["id"]
        assert staff.remove_sync(row_id) is True
        assert staff.all_sync() == []
        assert staff.remove_sync(row_id) is False


class TestResolve:
    def test_directory_beats_the_telegram_profile(self, tmp_paths):
        staff.add_sync("Микула Татьяна", "@Tatyana_Mikula")
        assert staff.resolve(555, "Tatyana_Mikula", "Tanya 🌸") == "Микула Татьяна"

    def test_unknown_person_keeps_their_profile_name(self, tmp_paths):
        """Справочник вторичен: подачу заявки он ломать не должен."""
        assert staff.resolve(555, "nobody", "Кто-то Новый") == "Кто-то Новый"

    def test_env_mapping_still_works(self, tmp_paths, monkeypatch):
        monkeypatch.setattr(settings, "employee_names_raw", "555:Старый Способ")
        settings.__dict__.pop("employee_names", None)
        assert staff.resolve(555, "nobody", "Профиль") == "Старый Способ"

    def test_id_is_learned_on_first_use_and_then_wins(self, tmp_paths):
        """@username человек сменит, id — нет: запоминаем при первой заявке."""
        staff.add_sync("Микула Татьяна", "@Tatyana_Mikula")
        staff.resolve(555, "Tatyana_Mikula", "—")
        assert staff.all_sync()[0]["tg_id"] == 555
        # Сменил ник — по id всё равно узнаём.
        assert staff.resolve(555, "new_handle", "Профиль") == "Микула Татьяна"

    def test_case_of_the_account_does_not_matter(self, tmp_paths):
        staff.add_sync("Петров Пётр", "@Petya")
        assert staff.resolve(1, "PETYA", "—") == "Петров Пётр"


class TestImport:
    def test_import_merges_and_reports_the_rest(self, tmp_paths):
        """Слияние, а не замена: добавленных руками не теряем."""
        staff.add_sync("Уволенный Кто-то", "@gone_away")
        rows, _ = staff.parse(REAL)
        out = staff.import_rows(rows)
        assert out["added"] == 3 and out["updated"] == 0
        assert out["not_in_sheet"] == ["Уволенный Кто-то"]
        assert len(staff.all_sync()) == 4

    def test_second_import_updates_and_adds_nothing(self, tmp_paths):
        rows, _ = staff.parse(REAL)
        staff.import_rows(rows)
        out = staff.import_rows(rows)
        assert out["added"] == 0 and out["updated"] == 3


class TestBadIds:
    """Поймано на живой проверке: нулевой id совпадал с кем угодно."""

    def test_zero_id_never_matches_anyone(self, tmp_paths):
        staff.add_sync("Петров Пётр", "@petya_p")
        staff.resolve(0, "petya_p", "—")          # раньше это писало tg_id = 0
        assert staff.all_sync()[0]["tg_id"] is None
        # …и любой незнакомец получал ФИО этой записи.
        assert staff.resolve(0, "кто_то", "Профиль") == "Профиль"

    def test_id_already_taken_does_not_break_submission(self, tmp_paths):
        """Один id на двух записях — повод для предупреждения, не для отказа."""
        staff.add_sync("Петров Пётр", "@petya_p", 555)
        staff.add_sync("Иванов Иван", "@ivan_i")
        assert staff.resolve(555, "ivan_i", "Профиль") == "Петров Пётр"
        rows = {r["full_name"]: r["tg_id"] for r in staff.all_sync()}
        assert rows == {"Петров Пётр": 555, "Иванов Иван": None}

    def test_negative_id_is_not_stored(self, tmp_paths):
        staff.add_sync("Петров Пётр", "@petya_p", -100)
        assert staff.all_sync()[0]["tg_id"] is None


class TestWhoSubmitted:
    """«Не подавали заявок» должно значить именно это.

    Считалось по наличию числового id, а он проставлялся только при подаче
    ПОСЛЕ появления справочника: сразу после импорта выходило, что не подавал
    никто — включая тех, у кого заявки в реестре есть.
    """

    async def test_id_is_linked_from_what_the_bot_already_knows(self, tmp_paths):
        from services import user_directory

        user_directory.remember(555, "petya_p")
        staff.add_sync("Петров Пётр", "@petya_p")
        items = await staff.listing()
        assert items[0]["tg_id"] == 555

    async def test_submitted_comes_from_the_audit_not_from_the_id(self, tmp_paths):
        from services import audit, user_directory

        user_directory.remember(555, "petya_p")
        user_directory.remember(777, "masha_m")
        staff.add_sync("Петров Пётр", "@petya_p")
        staff.add_sync("Маша М.", "@masha_m")
        audit.log_event_sync(audit.REQUEST_SUBMITTED, 555, "@petya_p",
                             "INV-1 · 100.00 RUB · ООО «Ромашка»")
        by_name = {i["full_name"]: i["submitted"] for i in await staff.listing()}
        assert by_name == {"Петров Пётр": True, "Маша М.": False}

    async def test_closing_documents_are_not_a_submission(self, tmp_paths):
        """Их когда-то писали тем же событием — человек считался подавшим."""
        from services import audit, user_directory

        user_directory.remember(555, "petya_p")
        staff.add_sync("Петров Пётр", "@petya_p")
        audit.log_event_sync(audit.REQUEST_SUBMITTED, 555, "@petya_p",
                             "INV-1: закрывающих документов +1")
        assert (await staff.listing())[0]["submitted"] is False


class TestBackfill:
    """Старые строки реестра подписаны именем из профиля Telegram."""

    async def test_rewrites_only_the_name_in_brackets(self, tmp_paths):
        from datetime import date

        from services import storage
        from tests.conftest import make_request

        await storage.append_invoice(make_request(
            request_id="INV-20260825-100000-0001",
            sender_username="@valentina_stan", sender_name="Valentina",
            planned_date=date(2026, 9, 1),
        ))
        staff.add_sync("Станиславчук Валентина", "@valentina_stan")
        out = await staff.backfill_registry()
        assert out["changed"] == 1
        assert out["examples"] == ["Valentina → Станиславчук Валентина"]
        row = await storage.get_request("INV-20260825-100000-0001")
        assert row["Сотрудник по заявке"] == "@valentina_stan (Станиславчук Валентина)"

    async def test_leaves_strangers_and_correct_rows_alone(self, tmp_paths):
        from datetime import date

        from services import storage
        from tests.conftest import make_request

        await storage.append_invoice(make_request(
            request_id="INV-20260825-100000-0002",
            sender_username="@nobody_here", sender_name="Кто-то",
            planned_date=date(2026, 9, 1),
        ))
        staff.add_sync("Станиславчук Валентина", "@valentina_stan")
        assert (await staff.backfill_registry())["changed"] == 0
