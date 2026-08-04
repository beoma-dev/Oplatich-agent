"""Права админа бота: ADMIN_IDS и администраторы доверенных чатов."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.access as access
from config import settings
from services import runtime_settings as rs


@pytest.fixture(autouse=True)
def _clean_admin_cache(monkeypatch):
    monkeypatch.setattr(access, "_admin_cache", {})


def _bot_with_status(status: str | Exception):
    bot = MagicMock()
    if isinstance(status, Exception):
        bot.get_chat_member = AsyncMock(side_effect=status)
    else:
        member = MagicMock()
        member.status = status
        bot.get_chat_member = AsyncMock(return_value=member)
    return bot


async def test_env_admin_wins_without_api_calls(tmp_paths, monkeypatch):
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", "42")
    bot = _bot_with_status("member")
    assert await access.is_bot_admin(bot, 42)
    bot.get_chat_member.assert_not_called()


async def test_channel_admin_gets_rights(tmp_paths):
    rs.remember_admin_chat(-100123, "Канал компании")
    assert await access.is_bot_admin(_bot_with_status("administrator"), 777)
    assert await access.is_bot_admin(_bot_with_status("creator"), 778)


async def test_plain_member_denied(tmp_paths):
    rs.remember_admin_chat(-100123, "Канал компании")
    assert not await access.is_bot_admin(_bot_with_status("member"), 779)


async def test_no_trusted_chats_denied(tmp_paths):
    assert not await access.is_bot_admin(_bot_with_status("administrator"), 780)


async def test_api_errors_do_not_grant_rights(tmp_paths):
    rs.remember_admin_chat(-100123, "Канал компании")
    assert not await access.is_bot_admin(_bot_with_status(RuntimeError("bot kicked")), 781)


async def test_result_is_cached(tmp_paths):
    rs.remember_admin_chat(-100123, "Канал компании")
    bot = _bot_with_status("administrator")
    assert await access.is_bot_admin(bot, 782)
    assert await access.is_bot_admin(bot, 782)
    assert bot.get_chat_member.await_count == 1  # второй раз — из кэша


def test_forget_admin_chat(tmp_paths):
    rs.remember_admin_chat(-100123, "Канал")
    assert rs.admin_chat_ids() == [-100123]
    assert rs.forget_admin_chat(-100123)
    assert rs.admin_chat_ids() == []
    assert not rs.forget_admin_chat(-100123)


class TestFinancierRemoval:
    """Убрать себя из финансистов можно и тогда, когда запись пришла из .env."""

    def test_env_financier_can_be_disabled(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555, @boss")

        assert rs.effective_finance_recipients() == ["555", "@boss"]
        assert rs.remove_financier("555") is True
        assert rs.effective_finance_recipients() == ["@boss"]

    def test_disabled_env_financier_comes_back_on_add(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")

        rs.remove_financier("555")
        assert rs.effective_finance_recipients() == []
        assert rs.add_financier("555") is True
        assert rs.effective_finance_recipients() == ["555"]

    def test_removing_unknown_changes_nothing(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        assert rs.remove_financier("999") is False
        assert rs.effective_finance_recipients() == ["555"]

    def test_username_case_is_ignored(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "@Boss")
        assert rs.remove_financier("@boss") is True
        assert rs.effective_finance_recipients() == []

    def test_panel_stops_showing_removed_financier(self, tmp_paths, monkeypatch):
        """Именно этого ждёт пользователь: убрал себя — панель пропала."""
        from bot.access import is_financier

        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        assert is_financier(555) is True
        rs.remove_financier("555")
        assert is_financier(555) is False
