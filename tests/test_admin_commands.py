"""Права на команды бота в чате: /admin, /allow, /export, /backup и прочие.

Появились после проверки диверсией: подмена `_admin_gate` на «пускать всех»
не роняла ни одного из 494 тестов, хотя за этим заслоном лежат выгрузка
реестра, архив всех данных и раздача доступа. Проверяем не только ответ
самой функции, но и ПОСЛЕДСТВИЯ: чужой не должен получить файл и не должен
изменить списки.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.access as access
import bot.admin as admin
from config import settings
from services import audit
from services import runtime_settings as rs

ADMIN_ID = 42
STRANGER_ID = 777


@pytest.fixture(autouse=True)
def _clean(tmp_paths, monkeypatch):
    monkeypatch.setattr(access, "_admin_cache", {})
    yield


def _admins(monkeypatch, raw: str) -> None:
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", raw)


def _update(user_id: int, *, chat_type: str = "private", text: str = "/admin") -> MagicMock:
    u = MagicMock()
    u.effective_user.id = user_id
    u.effective_user.username = "tester"
    u.effective_user.full_name = "Тест Тестов"
    u.effective_chat.type = chat_type
    u.effective_message.text = text
    u.effective_message.reply_text = AsyncMock()
    u.effective_message.reply_document = AsyncMock()
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(side_effect=RuntimeError("нет доверенных чатов"))
    u.get_bot = MagicMock(return_value=bot)
    return u


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.args = []
    return ctx


class TestGate:
    async def test_admin_in_private_passes(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        assert await admin._admin_gate(_update(ADMIN_ID)) is True

    async def test_stranger_is_refused_and_logged(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        upd = _update(STRANGER_ID)
        assert await admin._admin_gate(upd) is False
        assert "только администраторам" in upd.effective_message.reply_text.call_args[0][0]
        events = [e["event"] for e in await audit.recent_events(10)]
        assert audit.ADMIN_DENIED in events, "отказ обязан попадать в журнал"

    async def test_admin_in_group_is_refused_silently(self, monkeypatch):
        """В группе админ-команды не работают и не отвечают.

        Ответ в общий чат выдал бы состав админов и настройки посторонним.
        """
        _admins(monkeypatch, str(ADMIN_ID))
        upd = _update(ADMIN_ID, chat_type="group")
        assert await admin._admin_gate(upd) is False
        upd.effective_message.reply_text.assert_not_called()

    async def test_without_admins_explains_how_to_set_them(self, monkeypatch):
        """Свежая установка: некому и нечего запрещать — подсказываем."""
        _admins(monkeypatch, "")
        upd = _update(STRANGER_ID)
        assert await admin._admin_gate(upd) is False
        assert "ADMIN_IDS" in upd.effective_message.reply_text.call_args[0][0]


class TestConsequences:
    """Мало вернуть False — команда не должна ничего сделать."""

    async def test_export_gives_no_registry_to_a_stranger(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        upd = _update(STRANGER_ID, text="/export")
        await admin.export_command(upd, _ctx())
        upd.effective_message.reply_document.assert_not_called()

    async def test_backup_is_not_even_started_by_a_stranger(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        called = False

        async def _run(*_a, **_k):
            nonlocal called
            called = True
            raise AssertionError("бэкап не должен запускаться посторонним")

        from services import backup

        monkeypatch.setattr(backup, "run_backup", _run)
        await admin.backup_command(_update(STRANGER_ID, text="/backup"), _ctx())
        assert called is False

    async def test_stranger_cannot_open_access_to_anyone(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        before = list(rs.effective_allowed_ids())
        upd = _update(STRANGER_ID, text="/allow 999")
        ctx = _ctx()
        ctx.args = ["999"]
        await admin.allow_command(upd, ctx)
        assert rs.effective_allowed_ids() == before, "посторонний раздал доступ"

    async def test_stranger_cannot_add_a_financier(self, monkeypatch):
        _admins(monkeypatch, str(ADMIN_ID))
        before = list(rs.effective_finance_recipients())
        upd = _update(STRANGER_ID, text="/fin_add @vasya")
        ctx = _ctx()
        ctx.args = ["@vasya"]
        await admin.fin_add_command(upd, ctx)
        assert rs.effective_finance_recipients() == before

    async def test_admin_still_gets_the_registry(self, monkeypatch, tmp_path):
        """Обратная сторона: свой должен пройти, иначе тест бессмыслен."""
        _admins(monkeypatch, str(ADMIN_ID))
        path = tmp_path / "Реестр.xlsx"
        path.write_bytes("xlsx-содержимое".encode())
        monkeypatch.setattr(
            "services.storage.registry_export_path", lambda: path
        )
        upd = _update(ADMIN_ID, text="/export")
        await admin.export_command(upd, _ctx())
        upd.effective_message.reply_document.assert_called_once()
