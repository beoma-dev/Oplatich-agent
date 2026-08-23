"""Эксплуатационный контур: алерты, автобэкап, единое сообщение финансисту."""
from __future__ import annotations

import tarfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import services.alerts as alerts
from config import settings
from services import backup, notifier
from tests.conftest import make_request


@pytest.fixture(autouse=True)
def _reset_alerts(monkeypatch):
    monkeypatch.setattr(alerts, "_last_by_signature", {})
    monkeypatch.setattr(alerts, "_sent_times", [])


def _admins(monkeypatch, raw: str) -> None:
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", raw)


class TestAlerts:
    def test_same_signature_throttled(self):
        assert alerts._allowed("sig-a", now=0.0)
        assert not alerts._allowed("sig-a", now=100.0)      # окно 30 минут
        assert alerts._allowed("sig-a", now=2000.0)          # окно прошло
        assert alerts._allowed("sig-b", now=100.0)           # другая сигнатура

    def test_global_cap(self):
        for i in range(alerts.GLOBAL_MAX):
            assert alerts._allowed(f"sig-{i}", now=float(i))
        assert not alerts._allowed("sig-new", now=50.0)      # потолок за час

    async def test_delivery_to_all_admins(self, tmp_paths, monkeypatch):
        _admins(monkeypatch, "1,2")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        delivered = await alerts.alert_admins(bot, "Тест <b>", "детали <i>")
        assert delivered == 2
        text = bot.send_message.call_args.kwargs["text"]
        assert "&lt;b&gt;" in text and "&lt;i&gt;" in text   # HTML экранирован

    async def test_no_admins_no_send(self, tmp_paths, monkeypatch):
        _admins(monkeypatch, "")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        assert await alerts.alert_admins(bot, "Тест") == 0
        bot.send_message.assert_not_called()


class TestBackup:
    def test_seconds_until(self):
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 8, 4, 3, 0, tzinfo=tz)
        assert backup._seconds_until("03:30", now) == 1800
        assert backup._seconds_until("03:00", now) == 86400  # уже наступило → завтра

    def test_create_backup_contains_data(self, tmp_paths):
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        (settings.storage_path / "registry.xlsx").write_bytes(b"xlsx")
        settings.security_db_path.write_bytes(b"sqlite")

        path = backup.create_backup_sync()
        with tarfile.open(path) as tar:
            names = tar.getnames()
        assert "storage/registry.xlsx" in names
        assert "security.db" in names

    def test_rotation_keeps_last_n(self, tmp_paths, monkeypatch):
        bdir = backup.backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (bdir / f"invoice-bot-backup-2026080{i}-000000.tar.gz").write_bytes(b"x")
        backup._rotate_sync(2)
        left = sorted(p.name for p in bdir.glob("*.tar.gz"))
        assert len(left) == 2 and left[-1].startswith("invoice-bot-backup-20260804")


class TestSingleFinanceMessage:
    async def _notify(self, monkeypatch, *, invoice_file, pdf, requisites=""):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        r = make_request(
            has_invoice=invoice_file is not None,
            requisites=requisites,
            file_name="invoice.pdf" if invoice_file else "",
        )
        delivered = await notifier.notify_finance(
            bot, r, 1, pdf=pdf, invoice_file=invoice_file
        )
        return bot, delivered

    async def test_invoice_file_becomes_single_document(self, tmp_paths, monkeypatch):
        bot, delivered = await self._notify(monkeypatch, invoice_file=b"PDFDATA", pdf=b"%PDF")
        assert delivered == 1
        bot.send_message.assert_not_called()                  # ровно одно сообщение
        assert bot.send_document.await_count == 1
        kwargs = bot.send_document.call_args.kwargs
        assert kwargs["document"] == b"PDFDATA"               # приоритет — счёт
        assert kwargs["reply_markup"] is not None             # кнопки на документе
        assert "Сумма" in kwargs["caption"]

    async def test_requisites_request_attaches_pdf(self, tmp_paths, monkeypatch):
        bot, _ = await self._notify(
            monkeypatch, invoice_file=None, pdf=b"%PDF", requisites="ИНН 7707083893"
        )
        assert bot.send_document.call_args.kwargs["document"] == b"%PDF"

    async def test_caption_fits_telegram_limit(self, tmp_paths, monkeypatch):
        bot, _ = await self._notify(
            monkeypatch, invoice_file=None, pdf=b"%PDF", requisites="Х" * 1500
        )
        assert len(bot.send_document.call_args.kwargs["caption"]) <= 1024

    async def test_no_attachments_falls_back_to_text(self, tmp_paths, monkeypatch):
        bot, delivered = await self._notify(monkeypatch, invoice_file=None, pdf=None)
        assert delivered == 1
        bot.send_document.assert_not_called()
        assert bot.send_message.call_args.kwargs["reply_markup"] is not None


class TestCardCleanup:
    """Карточки удалённой заявки не должны копиться вечно."""

    async def test_cards_are_forgotten_with_the_request(self, tmp_paths):
        from services import cards

        await cards.save("INV-0001", chat_id=7, message_id=1, is_caption=False,
                         base_html="<b>карточка</b>")
        await cards.save("INV-0001", chat_id=8, message_id=2, is_caption=False,
                         base_html="<b>карточка</b>")
        await cards.save("INV-0002", chat_id=7, message_id=3, is_caption=False,
                         base_html="<b>чужая</b>")
        assert len(await cards.for_request("INV-0001")) == 2

        assert await cards.delete_for_request("INV-0001") == 2
        assert await cards.for_request("INV-0001") == []
        # Соседняя заявка не пострадала.
        assert len(await cards.for_request("INV-0002")) == 1

    async def test_deleting_a_request_clears_its_cards(self, tmp_paths, monkeypatch):
        """Полный путь: заявку удалили — адреса карточек забыли."""
        from unittest.mock import AsyncMock, MagicMock

        from services import cards, deletion, storage
        from tests.conftest import make_request

        request = make_request()
        await storage.append_invoice(request)
        await cards.save(request.request_id, chat_id=7, message_id=1,
                         is_caption=False, base_html="<b>к</b>")
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.edit_message_caption = AsyncMock()

        done, _msg = await deletion.delete_request(
            bot, request.request_id, actor_id=1, actor_name="админ", is_admin=True
        )
        assert done is True
        assert await cards.for_request(request.request_id) == []
