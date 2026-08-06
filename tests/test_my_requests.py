"""«Мои заявки»: выборки автора, справочник контрагентов, отзыв и повтор."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot.finance_actions as fa
import bot.my_requests as my
from config import settings
from services import cards, notifier, registry_sqlite, request_meta, storage
from services.deletion import delete_request
from services.withdraw import withdraw_request
from tests.conftest import make_request


def _rid(n: int) -> str:
    """Уникальный ID заявки: в реестре стоит UNIQUE по нему."""
    return f"INV-20260804-1000{n:02d}-000{n}"


def _bot() -> MagicMock:
    bot = MagicMock()
    message = MagicMock()
    message.chat.id = 555
    message.message_id = 77
    bot.send_document = AsyncMock(return_value=message)
    bot.send_message = AsyncMock(return_value=message)
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


class TestAuthorQueries:
    async def test_only_own_requests_newest_first(self, tmp_paths):
        mine_old = make_request(telegram_id=100, counterparty="Первый", request_id=_rid(1))
        alien = make_request(telegram_id=200, counterparty="Чужой", request_id=_rid(2))
        mine_new = make_request(telegram_id=100, counterparty="Второй", request_id=_rid(3))
        for r in (mine_old, alien, mine_new):
            await storage.append_invoice(r)

        rows = await storage.recent_by_author(100)
        assert [r["Контрагент"] for r in rows] == ["Второй", "Первый"]

    async def test_limit_respected(self, tmp_paths):
        for i in range(5):
            await storage.append_invoice(
                make_request(telegram_id=7, counterparty=f"К{i}", request_id=_rid(i))
            )
        assert len(await storage.recent_by_author(7, limit=3)) == 3

    async def test_get_request_by_id(self, tmp_paths):
        r = make_request(telegram_id=100)
        await storage.append_invoice(r)
        row = await storage.get_request(r.request_id)
        assert row is not None
        assert row["Telegram ID"] == "100"
        assert await storage.get_request("INV-20260101-000000-0000") is None


class TestCounterpartyBook:
    async def test_book_keeps_last_known_requisites(self, tmp_paths):
        # У одного контрагента сперва счёт (реквизитов нет), затем реквизиты.
        await storage.append_invoice(make_request(
            counterparty="ООО «Ромашка»", has_invoice=True, requisites="", request_id=_rid(1)
        ))
        await storage.append_invoice(make_request(
            counterparty="ООО «Ромашка»", requisites="ИНН 7707083893", request_id=_rid(2)
        ))
        await storage.append_invoice(make_request(
            counterparty="ИП Петров", requisites="", request_id=_rid(3)
        ))

        book = await storage.counterparty_book(limit=6)
        by_name = {it["name"]: it["requisites"] for it in book}
        assert by_name["ООО «Ромашка»"] == "ИНН 7707083893"
        assert by_name["ИП Петров"] == ""
        # Частый контрагент — выше в списке подсказок.
        assert book[0]["name"] == "ООО «Ромашка»"

    def test_sqlite_book_ignores_blank_names(self, tmp_paths):
        registry_sqlite.import_row_sync(
            ["2026-08-04 10:00", "", "@a", "  ", "10", "", "Новая", "", "", "RUB",
             "Обычная", "", "INV-20260804-100000-0001", "1"]
        )
        assert registry_sqlite.counterparty_book_sync(6) == []


class TestWithdraw:
    async def _submitted(self, monkeypatch, *, author: int = 901):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        bot = _bot()
        r = make_request(telegram_id=author)
        await storage.append_invoice(r)
        await notifier.notify_finance(bot, r, 1, pdf=b"%PDF")
        return bot, r

    async def test_author_withdraws_new_request(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        ok, message = await withdraw_request(
            bot, r.request_id, actor_id=901, actor_name="@author"
        )
        assert ok, message

        row = await storage.get_request(r.request_id)
        assert row["Статус оплаты"] == "Отозвана"
        # Карточка финансиста закрыта: текст обновлён, кнопок больше нет.
        kwargs = bot.edit_message_caption.call_args.kwargs
        assert "Отозвана автором" in kwargs["caption"]
        assert kwargs["reply_markup"] is None

    async def test_financiers_get_a_message_not_only_edited_card(
        self, tmp_paths, monkeypatch
    ):
        """Карточка могла уехать вверх чата — нужен отдельный сигнал."""
        bot, r = await self._submitted(monkeypatch)
        bot.send_message.reset_mock()

        assert (await withdraw_request(
            bot, r.request_id, actor_id=901, actor_name="@author"
        ))[0]

        sent = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 555]
        assert len(sent) == 1
        text = sent[0].kwargs["text"]
        assert "отозвана автором" in text.lower()
        assert r.counterparty in text
        assert "Оплачивать не нужно" in text

    async def test_notification_failure_does_not_break_withdrawal(
        self, tmp_paths, monkeypatch
    ):
        bot, r = await self._submitted(monkeypatch)
        bot.send_message = AsyncMock(side_effect=RuntimeError("чат недоступен"))

        ok, _ = await withdraw_request(
            bot, r.request_id, actor_id=901, actor_name="@author"
        )
        assert ok
        row = await storage.get_request(r.request_id)
        assert row["Статус оплаты"] == "Отозвана"

    async def test_alien_request_refused(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        ok, message = await withdraw_request(
            bot, r.request_id, actor_id=777, actor_name="@stranger"
        )
        assert not ok
        assert "только свою" in message
        row = await storage.get_request(r.request_id)
        assert row["Статус оплаты"] == "Новая"

    async def test_processed_request_refused(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        await storage.set_request_status(r.request_id, "Оплачена")
        ok, message = await withdraw_request(
            bot, r.request_id, actor_id=901, actor_name="@author"
        )
        assert not ok
        assert "Оплачена" in message

    async def test_unknown_request(self, tmp_paths):
        ok, message = await withdraw_request(
            _bot(), "INV-20260101-000000-0000", actor_id=1, actor_name="@a"
        )
        assert not ok
        assert "не найдена" in message

    async def test_financier_cannot_change_withdrawn(self, tmp_paths, monkeypatch):
        monkeypatch.setattr(fa, "_pending_reasons", {})
        bot, r = await self._submitted(monkeypatch)
        await withdraw_request(bot, r.request_id, actor_id=901, actor_name="@author")

        query = MagicMock()
        query.data = f"ST:{r.request_id}:PAID"
        query.answer = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = 555
        update.effective_user.username = "fin"
        ctx = MagicMock()
        ctx.bot = bot

        await fa.status_callback(update, ctx)
        assert "отозвана" in query.answer.call_args.args[0].lower()
        row = await storage.get_request(r.request_id)
        assert row["Статус оплаты"] == "Отозвана"


class TestReasonStored:
    async def test_reason_survives_for_the_author(self, tmp_paths, monkeypatch):
        monkeypatch.setattr(fa, "_pending_reasons", {})
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        bot = _bot()
        r = make_request(telegram_id=901)
        await storage.append_invoice(r)
        await notifier.notify_finance(bot, r, 1, pdf=b"%PDF")

        from services.status_change import apply_status

        await apply_status(
            bot,
            r.request_id,
            "REJECTED",
            actor_id=555,
            actor_name="@fin",
            reason="нет бюджета",
        )
        assert await request_meta.reasons_for([r.request_id]) == {
            r.request_id: "нет бюджета"
        }


class TestChatList:
    def test_amount_formatting(self):
        assert my.format_amount("125000.50") == "125 000.50"
        assert my.format_amount("мусор") == "мусор"

    def test_withdraw_button_only_for_new(self):
        rows = [
            {"ID заявки": "INV-20260804-100000-0001", "Статус оплаты": "Новая"},
            {"ID заявки": "INV-20260804-100000-0002", "Статус оплаты": "Оплачена"},
        ]
        markup = my._build_keyboard(rows)
        first, second = markup.inline_keyboard
        assert [b.text for b in first] == ["↻ Повторить №1", "🚫 Отозвать №1"]
        assert [b.text for b in second] == ["↻ Повторить №2"]

    def test_list_shows_status_and_reason(self):
        rows = [{
            "ID заявки": "INV-20260804-100000-0001",
            "Статус оплаты": "Отклонена",
            "Контрагент": "ООО «Ромашка»",
            "Сумма": "125000.50",
            "Валюта": "RUB",
            "Плановая дата оплаты": "15.08.2026",
        }]
        text = my._format_list(rows, {"INV-20260804-100000-0001": "нет бюджета"})
        assert "❌" in text and "Отклонена" in text
        assert "125 000.50 RUB" in text
        assert "нет бюджета" in text

    async def test_group_chat_does_not_leak_list(self, tmp_paths):
        update = MagicMock()
        update.effective_chat.type = "supergroup"
        update.effective_message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot.username = "invoice_bot"

        await my.my_command(update, ctx)
        text = update.effective_message.reply_text.call_args.args[0]
        assert "в личке" in text

    async def test_empty_list_hint(self, tmp_paths, monkeypatch):
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "42")
        update = MagicMock()
        update.effective_chat.type = "private"
        update.effective_user.id = 42
        update.effective_message.reply_text = AsyncMock()

        await my.my_command(update, MagicMock())
        assert "Заявок пока нет" in update.effective_message.reply_text.call_args.args[0]


class TestCardsUpdateAll:
    async def test_updates_every_financier_card(self, tmp_paths):
        await cards.save("INV-1", 10, 100, is_caption=False, base_html="карточка")
        await cards.save("INV-1", 20, 200, is_caption=False, base_html="карточка")
        bot = _bot()
        updated = await cards.update_all(bot, "INV-1", "\n\nстатус")
        assert updated == 2
        assert bot.edit_message_text.await_count == 2

    async def test_fallback_used_when_no_saved_cards(self, tmp_paths):
        bot = _bot()
        fallback = {
            "chat_id": 5, "message_id": 6, "is_caption": True, "base_html": "карточка"
        }
        assert await cards.update_all(bot, "INV-нет", "\n\nстатус", fallback=fallback) == 1
        assert bot.edit_message_caption.await_count == 1


@pytest.mark.parametrize(
    "status,icon", [("Новая", "⏳"), ("Оплачена", "✅"), ("Отозвана", "🚫")]
)
def test_status_icons(status, icon):
    assert my._icon(status) == icon


class TestChatRepeat:
    """Повтор заявки в чат-форме (режим без Mini App)."""

    def _query(self, request_id: str, user_id: int = 100):
        update = MagicMock()
        q = update.callback_query
        q.data = f"{my.CB_REPEAT}:{request_id}"
        q.answer = AsyncMock()
        q.message.reply_text = AsyncMock()
        update.effective_user.id = user_id
        update.effective_user.username = "author"
        return update

    async def test_prefills_fields_and_asks_about_invoice(self, tmp_paths, monkeypatch):
        from bot import handlers

        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "100")
        monkeypatch.setattr(settings, "webapp_url", "")

        r = make_request(telegram_id=100, counterparty="ООО «Ромашка»")
        await storage.append_invoice(r)

        ctx = MagicMock()
        ctx.user_data = {}
        state = await handlers.repeat_start(self._query(r.request_id), ctx)

        assert state == handlers.INVOICE_CHOICE
        assert ctx.user_data[handlers.K_COUNTERPARTY] == "ООО «Ромашка»"
        assert str(ctx.user_data[handlers.K_AMOUNT]) == "125000.50"
        assert ctx.user_data[handlers.K_CURRENCY] == "RUB"
        # Плановая дата считается заново — прошлая уже в прошлом.
        assert ctx.user_data[handlers.K_PLANNED] >= r.created_at.date()

    async def test_alien_request_not_repeatable(self, tmp_paths, monkeypatch):
        from telegram.ext import ConversationHandler

        from bot import handlers

        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "100, 200")
        monkeypatch.setattr(settings, "webapp_url", "")

        r = make_request(telegram_id=200)
        await storage.append_invoice(r)

        update = self._query(r.request_id, user_id=100)
        ctx = MagicMock()
        ctx.user_data = {}
        state = await handlers.repeat_start(update, ctx)

        assert state == ConversationHandler.END
        assert ctx.user_data == {}
        assert "не найдена" in update.callback_query.message.reply_text.call_args.args[0]

    def test_repeat_button_is_webapp_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "webapp_url", "https://example.org/")
        rows = [{"ID заявки": "INV-20260804-100000-0001", "Статус оплаты": "Новая"}]
        button = my._build_keyboard(rows).inline_keyboard[0][0]
        assert button.web_app is not None
        assert button.web_app.url.endswith("?repeat=INV-20260804-100000-0001")
        assert button.callback_data is None


class TestFinanceQueries:
    async def test_recent_requests_covers_all_authors(self, tmp_paths):
        await storage.append_invoice(
            make_request(telegram_id=1, counterparty="Первый", request_id=_rid(1))
        )
        await storage.append_invoice(
            make_request(telegram_id=2, counterparty="Второй", request_id=_rid(2))
        )
        rows = await storage.recent_requests(limit=10)
        assert [r["Контрагент"] for r in rows] == ["Второй", "Первый"]

    async def test_limit_is_respected(self, tmp_paths):
        for i in range(4):
            await storage.append_invoice(
                make_request(telegram_id=i, counterparty=f"К{i}", request_id=_rid(i))
            )
        assert len(await storage.recent_requests(limit=2)) == 2


class TestDeletion:
    """Удаление необратимо: автор — только свою отозванную, админ — любую."""

    async def _submitted(self, monkeypatch, *, author: int = 901):
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "555")
        bot = _bot()
        r = make_request(telegram_id=author)
        await storage.append_invoice(r)
        await notifier.notify_finance(bot, r, 1, pdf=b"%PDF")
        return bot, r

    async def test_author_cannot_delete_new_request(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        ok, message = await delete_request(
            bot, r.request_id, actor_id=901, actor_name="@a", is_admin=False
        )
        assert not ok
        assert "администратор" in message
        assert await storage.get_request(r.request_id) is not None

    async def test_author_deletes_own_withdrawn(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        await withdraw_request(bot, r.request_id, actor_id=901, actor_name="@a")

        ok, _ = await delete_request(
            bot, r.request_id, actor_id=901, actor_name="@a", is_admin=False
        )
        assert ok
        assert await storage.get_request(r.request_id) is None
        # Карточка финансиста закрывается пометкой.
        assert "Удалена из реестра" in bot.edit_message_caption.call_args.kwargs["caption"]

    async def test_alien_request_refused(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        await withdraw_request(bot, r.request_id, actor_id=901, actor_name="@a")
        ok, message = await delete_request(
            bot, r.request_id, actor_id=777, actor_name="@stranger", is_admin=False
        )
        assert not ok
        assert "только свою" in message
        assert await storage.get_request(r.request_id) is not None

    async def test_admin_deletes_any_status(self, tmp_paths, monkeypatch):
        bot, r = await self._submitted(monkeypatch)
        await storage.set_request_status(r.request_id, "Оплачена")
        ok, _ = await delete_request(
            bot, r.request_id, actor_id=1, actor_name="@admin", is_admin=True
        )
        assert ok
        assert await storage.get_request(r.request_id) is None

    async def test_unknown_request(self, tmp_paths):
        ok, message = await delete_request(
            _bot(), "INV-20260101-000000-0000", actor_id=1, actor_name="@a", is_admin=True
        )
        assert not ok
        assert "не найдена" in message

    async def test_deletion_is_audited(self, tmp_paths, monkeypatch):
        from services import audit

        bot, r = await self._submitted(monkeypatch)
        await withdraw_request(bot, r.request_id, actor_id=901, actor_name="@a")
        await delete_request(
            bot, r.request_id, actor_id=901, actor_name="@a", is_admin=False
        )
        events = [e["event"] for e in await audit.recent_events(20)]
        assert audit.REQUEST_DELETED in events

    async def test_mirror_row_is_removed_too(self, tmp_paths, monkeypatch):
        from services import registry_xlsx

        monkeypatch.setattr(settings, "registry_file", str(tmp_paths / "registry.xlsx"))
        bot, r = await self._submitted(monkeypatch)
        await withdraw_request(bot, r.request_id, actor_id=901, actor_name="@a")
        await delete_request(
            bot, r.request_id, actor_id=901, actor_name="@a", is_admin=False
        )
        rows = registry_xlsx.recent_counterparties_sync(10, settings.registry_path)
        assert r.counterparty not in rows


class TestGroupPost:
    async def test_post_tells_to_start_the_bot_first(self, tmp_paths):
        """Без /start бот не может писать в личку — предупреждаем в посте."""
        from bot import handlers

        update = MagicMock()
        update.effective_chat.id = -100500
        update.effective_chat.type = "supergroup"
        ctx = MagicMock()
        ctx.bot.username = "invoice_test_bot"
        ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        ctx.bot.pin_chat_message = AsyncMock()

        await handlers._post_group_button(update, ctx, pin=False)

        text = ctx.bot.send_message.call_args.kwargs["text"]
        assert "первый раз" in text.lower()
        assert "/start" in text
        assert "https://t.me/invoice_test_bot" in text


class TestAmountFormatting:
    """Суммы из Google приходят с неразрывным пробелом — не должны ломаться."""

    def test_non_breaking_space(self):
        assert my.format_amount("125 000,50") == "125 000.50"

    def test_plain_value(self):
        assert my.format_amount("8400.00") == "8 400.00"

    def test_unparsable_stays_as_is(self):
        assert my.format_amount("—") == "—"


class TestRegistryUnavailable:
    """Недоступный реестр не должен выглядеть как «заявок нет».

    Именно эта подмена однажды съела напоминания: пустой результат вместо
    ошибки. В «Моих заявках» она врёт человеку про его же заявки.
    """

    async def test_storage_can_raise_instead_of_lying(self, tmp_paths, monkeypatch):
        from services import registry_sqlite, storage

        def boom(*args, **kwargs):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(registry_sqlite, "recent_by_author_sync", boom)
        assert await storage.recent_by_author(42) == [], "мягкий режим прежний"
        with pytest.raises(storage.RegistryUnavailable):
            await storage.recent_by_author(42, strict=True)

    async def test_api_answers_503_not_empty_list(self, tmp_paths, monkeypatch):
        import httpx

        import api.routes as routes_mod
        import bot.access as access
        from api.server import build_api
        from services import registry_sqlite
        from tests.test_api_integration import _auth, _make_bot

        def boom(*args, **kwargs):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(registry_sqlite, "recent_by_author_sync", boom)
        monkeypatch.setattr(routes_mod, "_my_rate", {})
        monkeypatch.setattr(access, "_admin_cache", {})
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "42")

        transport = httpx.ASGITransport(app=build_api(_make_bot()))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/api/my-requests", headers=_auth())
        assert resp.status_code == 503
        assert "недоступен" in resp.json()["detail"]

    async def test_chat_says_registry_is_down(self, tmp_paths, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from bot import my_requests
        from services import registry_sqlite

        def boom(*args, **kwargs):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(registry_sqlite, "recent_by_author_sync", boom)
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "42")

        update, context = MagicMock(), MagicMock()
        update.effective_user.id = 42
        update.effective_chat.type = "private"
        update.effective_message.reply_text = AsyncMock()
        await my_requests.my_command(update, context)
        text = update.effective_message.reply_text.await_args[0][0]
        assert "Реестр сейчас недоступен" in text
        assert "Заявок пока нет" not in text
