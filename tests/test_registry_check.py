"""Сверка реестра с xlsx-зеркалом.

Поводом стало настоящее расхождение: в реестре ноль заявок, в файле,
который открывают финансисты, — две строки от давно удалённых. Сбой записи
в зеркало намеренно не отменяет заявку, поэтому разойтись они могут молча;
эти тесты держат сверку в рабочем состоянии.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import openpyxl
import pytest

from bot.models import SHEET_HEADERS, InvoiceRequest, Urgency
from config import settings
from services import alerts, registry_check, registry_xlsx, storage


def _req(i: int) -> InvoiceRequest:
    return InvoiceRequest(
        telegram_id=42, sender_username="@t", sender_name="Т",
        amount=Decimal("100.00"), currency="RUB", counterparty=f"Контрагент {i}",
        article="Аренда", urgency=Urgency.NORMAL, planned_date=date(2026, 9, 1),
        work_deadline="месяц", comment="—", file_name="s.pdf", file_url="/x",
        request_id=f"INV-{i:04d}",
    )


@pytest.fixture()
def mirror(tmp_paths):
    """Пустое зеркало по пути боевого реестра."""
    return settings.registry_path


class TestCheck:
    async def test_no_file_is_not_an_error(self, tmp_paths):
        result = await registry_check.check()
        assert result["checked"] is False and result["ok"] is True
        assert "нет" in result["reason"]

    async def test_matching_registry_and_mirror(self, mirror):
        for i in (1, 2, 3):
            await storage.append_invoice(_req(i))
        result = await registry_check.check()
        assert result["ok"] is True
        assert result["primary"] == 3 and result["mirror"] == 3
        assert "совпадают" in registry_check.describe(result)

    async def test_row_left_in_mirror_is_caught(self, mirror):
        """Ровно то, что случилось в бою: заявку удалили, строка осталась."""
        for i in (1, 2):
            await storage.append_invoice(_req(i))
        from services import registry_sqlite

        registry_sqlite.delete_sync("INV-0002")     # только источник правды
        result = await registry_check.check()
        assert result["ok"] is False
        assert result["extra_count"] == 1
        assert result["extra_in_mirror"] == ["INV-0002"]
        assert "лишних в зеркале — 1" in registry_check.describe(result)

    async def test_missing_row_in_mirror_is_caught(self, mirror):
        await storage.append_invoice(_req(1))
        registry_xlsx.delete_sync("INV-0001", mirror)   # только зеркало
        result = await registry_check.check()
        assert result["ok"] is False and result["missing_count"] == 1

    async def test_rows_without_id_are_reported(self, mirror):
        """Старый формат: сверить нечем, но и молчать нельзя."""
        await storage.append_invoice(_req(1))
        wb = openpyxl.load_workbook(mirror)
        ws = wb.active
        row = ["значение"] * len(SHEET_HEADERS)
        row[SHEET_HEADERS.index("ID заявки")] = ""
        ws.append(row)
        wb.save(mirror)
        result = await registry_check.check()
        assert result["ok"] is False and result["rows_without_id"] == 1
        assert "строк без ID — 1" in registry_check.describe(result)

    async def test_broken_file_does_not_break_the_panel(self, mirror, monkeypatch):
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_bytes(b"\x00 not xlsx")
        result = await registry_check.check()
        assert result["checked"] is False and result["ok"] is True


class TestAlert:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_paths, monkeypatch):
        # tmp_paths гасит ADMIN_IDS из боевого .env — назначаем ПОСЛЕ него.
        monkeypatch.setattr(alerts, "_last_by_signature", {})
        monkeypatch.setattr(alerts, "_sent_times", [])
        settings.__dict__.pop("admin_ids", None)
        monkeypatch.setattr(settings, "admin_ids_raw", "77")

    async def test_divergence_reaches_admins(self, mirror):
        await storage.append_invoice(_req(1))
        from services import registry_sqlite

        registry_sqlite.delete_sync("INV-0001")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        result = await registry_check.alert_if_diverged(bot)
        assert result["ok"] is False
        text = bot.send_message.call_args.kwargs["text"]
        assert "разошлись" in text and "INV-0001" in text

    async def test_silence_when_all_is_well(self, mirror):
        await storage.append_invoice(_req(1))
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await registry_check.alert_if_diverged(bot)
        bot.send_message.assert_not_called()

    async def test_kind_can_be_switched_off(self, mirror):
        """Расхождение — не потеря данных, значит категория выключаемая."""
        from services import runtime_settings as rs

        assert "mirror" not in rs.CRITICAL_ALERT_KEYS
        rs.set_alerts_config(kinds={"mirror": False})
        await storage.append_invoice(_req(1))
        from services import registry_sqlite

        registry_sqlite.delete_sync("INV-0001")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await registry_check.alert_if_diverged(bot)
        bot.send_message.assert_not_called()
        # Но в журнал инцидентов расхождение попало.
        assert any(i["kind"] == "mirror" for i in rs.recent_incidents())
