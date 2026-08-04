"""Напоминания о сроках: что попадает в сводку и кому уходит."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from config import settings
from services import reminders, storage
from tests.conftest import make_request


def _row(**over) -> dict[str, str]:
    row = {
        "Плановая дата оплаты": "05.08.2026",
        "Статус оплаты": "Новая",
        "Контрагент": "ООО «Ромашка»",
        "Сумма": "1000.00",
        "Валюта": "RUB",
        "Срочность": "Обычная",
        "Сотрудник по заявке": "@tester (Тест)",
    }
    row.update(over)
    return row


TODAY = date(2026, 8, 4)


class TestSplit:
    def test_tomorrow_goes_to_due(self):
        due, overdue = reminders.split_by_deadline([_row()], TODAY)
        assert len(due) == 1 and not overdue

    def test_past_date_goes_to_overdue(self):
        due, overdue = reminders.split_by_deadline(
            [_row(**{"Плановая дата оплаты": "01.08.2026"})], TODAY
        )
        assert not due and len(overdue) == 1

    def test_today_is_neither(self):
        """Сегодняшние ещё в работе — дёргать по ним рано."""
        due, overdue = reminders.split_by_deadline(
            [_row(**{"Плановая дата оплаты": "04.08.2026"})], TODAY
        )
        assert not due and not overdue

    def test_paid_and_withdrawn_are_ignored(self):
        rows = [
            _row(**{"Статус оплаты": "Оплачена", "Плановая дата оплаты": "01.08.2026"}),
            _row(**{"Статус оплаты": "Отозвана", "Плановая дата оплаты": "01.08.2026"}),
            _row(**{"Статус оплаты": "Отклонена", "Плановая дата оплаты": "01.08.2026"}),
        ]
        due, overdue = reminders.split_by_deadline(rows, TODAY)
        assert not due and not overdue

    def test_deferred_still_counts(self):
        """«Отложена» — не «сделано»: просрочка по ней остаётся просрочкой."""
        due, overdue = reminders.split_by_deadline(
            [_row(**{"Статус оплаты": "Отложена", "Плановая дата оплаты": "01.08.2026"})],
            TODAY,
        )
        assert len(overdue) == 1

    def test_unparsable_date_is_skipped(self):
        due, overdue = reminders.split_by_deadline(
            [_row(**{"Плановая дата оплаты": ""})], TODAY
        )
        assert not due and not overdue

    def test_iso_dates_are_understood(self):
        due, _ = reminders.split_by_deadline(
            [_row(**{"Плановая дата оплаты": "2026-08-05"})], TODAY
        )
        assert len(due) == 1


class TestMessages:
    def test_due_message_sums_by_currency(self):
        rows = [_row(), _row(**{"Сумма": "500.50"}), _row(**{"Валюта": "USD", "Сумма": "20"})]
        text = reminders.build_due_message(rows)
        assert "Завтра к оплате: 3" in text
        assert "1 500.50 RUB" in text
        assert "20.00 USD" in text

    def test_urgent_is_marked(self):
        text = reminders.build_due_message([_row(**{"Срочность": "Срочно"})])
        assert "🔴" in text

    def test_long_list_is_trimmed(self):
        text = reminders.build_due_message([_row() for _ in range(13)])
        assert "…и ещё 3" in text

    def test_overdue_message_shows_date_and_status(self):
        text = reminders.build_overdue_message(
            [_row(**{"Плановая дата оплаты": "01.08.2026", "Статус оплаты": "Отложена"})]
        )
        assert "Просрочено: 1" in text
        assert "01.08.2026" in text
        assert "Отложена" in text


class TestDelivery:
    async def test_due_to_financiers_overdue_to_admins(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        settings.__dict__.pop("admin_ids", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        monkeypatch.setattr(settings, "admin_ids_raw", "777")

        await storage.append_invoice(make_request(
            planned_date=date(2026, 8, 5), request_id="INV-20260804-100001-0001"
        ))
        await storage.append_invoice(make_request(
            planned_date=date(2026, 8, 1), request_id="INV-20260804-100002-0002"
        ))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        due, overdue = await reminders.run_reminders(bot, today=TODAY)

        assert (due, overdue) == (1, 1)
        by_chat = {c.kwargs["chat_id"]: c.kwargs["text"] for c in bot.send_message.await_args_list}
        assert "Завтра к оплате" in by_chat[555]
        assert "Просрочено" in by_chat[777]

    async def test_silent_when_nothing_to_remind(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        bot = MagicMock()
        bot.send_message = AsyncMock()

        assert await reminders.run_reminders(bot, today=TODAY) == (0, 0)
        bot.send_message.assert_not_awaited()

    async def test_unreachable_recipient_does_not_break_the_rest(
        self, tmp_paths, monkeypatch
    ):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555, 556")
        await storage.append_invoice(make_request(
            planned_date=date(2026, 8, 5), request_id="INV-20260804-100003-0003"
        ))

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("нет чата"), None])
        due, _ = await reminders.run_reminders(bot, today=TODAY)
        assert due == 1
        assert bot.send_message.await_count == 2


class TestConfigurableBehaviour:
    """Параметры из админ-панели меняют поведение рассылки."""

    def test_horizon_widens_with_days_before(self):
        rows = [
            _row(**{"Плановая дата оплаты": "05.08.2026"}),   # завтра
            _row(**{"Плановая дата оплаты": "07.08.2026"}),   # через 3 дня
        ]
        due, _ = reminders.split_by_deadline(rows, TODAY, days_before=1)
        assert len(due) == 1
        due, _ = reminders.split_by_deadline(rows, TODAY, days_before=3)
        assert len(due) == 2

    def test_zero_days_means_today_only(self):
        rows = [
            _row(**{"Плановая дата оплаты": "04.08.2026"}),   # сегодня
            _row(**{"Плановая дата оплаты": "05.08.2026"}),   # завтра
        ]
        due, _ = reminders.split_by_deadline(rows, TODAY, days_before=0)
        assert len(due) == 1
        assert due[0]["Плановая дата оплаты"] == "04.08.2026"

    def test_message_wording_follows_horizon(self):
        assert "Сегодня к оплате" in reminders.build_due_message([_row()], 0)
        assert "Завтра к оплате" in reminders.build_due_message([_row()], 1)
        assert "В ближайшие 3 дн." in reminders.build_due_message([_row()], 3)

    def test_overdue_recipients_by_target(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        settings.__dict__.pop("admin_ids", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        monkeypatch.setattr(settings, "admin_ids_raw", "777")

        assert reminders.overdue_recipients("admins") == [777]
        assert reminders.overdue_recipients("financiers") == [555]
        assert sorted(reminders.overdue_recipients("both")) == [555, 777]

    async def test_overdue_can_be_switched_off(self, tmp_paths, monkeypatch):
        from services import runtime_settings as rs

        settings.__dict__.pop("admin_ids", None)
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "admin_ids_raw", "777")
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        rs.set_reminders_config(overdue_enabled=False)

        await storage.append_invoice(make_request(
            planned_date=date(2026, 8, 1), request_id="INV-20260804-100009-0009"
        ))
        bot = MagicMock()
        bot.send_message = AsyncMock()
        due, overdue = await reminders.run_reminders(bot, today=TODAY)

        assert (due, overdue) == (0, 1)      # посчитали
        bot.send_message.assert_not_awaited()  # но не отправили

    async def test_disabled_config_is_reported_but_manual_run_works(
        self, tmp_paths, monkeypatch
    ):
        """«Проверить сейчас» шлёт независимо от расписания."""
        from services import runtime_settings as rs

        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        rs.set_reminders_config(enabled=False)

        await storage.append_invoice(make_request(
            planned_date=date(2026, 8, 5), request_id="INV-20260804-100010-0010"
        ))
        bot = MagicMock()
        bot.send_message = AsyncMock()
        due, _ = await reminders.run_reminders(bot, today=TODAY)
        assert due == 1
        bot.send_message.assert_awaited()


class TestAmountsFromSheets:
    """Google отдаёт суммы отформатированными — с НЕРАЗРЫВНЫМ пробелом.

    Регрессия: обычный .replace(" ", "") его не убирал, Decimal падал, и в
    сводке стояло «Сумма: 0.00 RUB» при непустом списке заявок.
    """

    def test_non_breaking_space_is_parsed(self):
        assert reminders._amount({"Сумма": "125 000,50"}) == Decimal("125000.50")

    def test_narrow_and_plain_spaces_too(self):
        assert reminders._amount({"Сумма": "125 000,50"}) == Decimal("125000.50")
        assert reminders._amount({"Сумма": "125 000,50"}) == Decimal("125000.50")

    def test_plain_decimal_still_works(self):
        assert reminders._amount({"Сумма": "8400.00"}) == Decimal("8400.00")

    def test_garbage_counts_as_zero(self):
        assert reminders._amount({"Сумма": "—"}) == Decimal(0)
        assert reminders._amount({}) == Decimal(0)

    def test_totals_line_is_not_zero_for_formatted_rows(self):
        rows = [
            _row(**{"Сумма": "125 000,50"}),
            _row(**{"Сумма": "32 450,00"}),
            _row(**{"Сумма": "300,00", "Валюта": "USD"}),
        ]
        text = reminders.build_due_message(rows)
        assert "0.00 RUB" not in text
        assert "157 450.50 RUB" in text
        assert "300.00 USD" in text
