"""Интеграционные тесты HTTP API: полный путь через ASGI, без сети.

Реальные: валидация, дедуп, rate limit, реестр, PDF, фасад хранилища.
Мок — только Telegram-бот (send_message/send_document/get_chat_member).
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import api.routes as routes_mod
import bot.access as access
from api.server import build_api
from config import settings
from tests.test_auth import _signed_init_data


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    member = MagicMock()
    member.status = "member"
    bot.get_chat_member = AsyncMock(return_value=member)
    return bot


@pytest.fixture()
async def api(tmp_paths, monkeypatch):
    # Оба счётчика лимитов — свои на каждый тест: иначе соседние тесты
    # выбирают квоту одного и того же user_id и получают 429.
    monkeypatch.setattr(routes_mod, "_rate", {})
    monkeypatch.setattr(routes_mod, "_my_rate", {})
    monkeypatch.setattr(routes_mod, "_check_rate", {})
    monkeypatch.setattr(access, "_admin_cache", {})
    bot = _make_bot()
    transport = httpx.ASGITransport(app=build_api(bot))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, bot


def _auth(user_id: int = 42) -> dict:
    return {
        "X-Telegram-Init-Data": _signed_init_data(
            user={"id": user_id, "first_name": "Тест", "username": "tester"}
        )
    }


def _allow(monkeypatch, ids: str = "42") -> None:
    settings.__dict__.pop("allowed_user_ids", None)
    monkeypatch.setattr(settings, "allowed_user_ids_raw", ids)


def _admins(monkeypatch, ids: str) -> None:
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", ids)


def _form(**overrides) -> dict:
    data = {
        "amount": "125 000,50",
        "currency": "RUB",
        "counterparty": "ООО «Ромашка»",
        "article": "Аренда",
        "planned_date": "auto",
        "comment": "аренда за июль",
        "urgency": "NORMAL",
        "has_invoice": "0",
        "requisites": "ИНН 7707083893",
    }
    data.update(overrides)
    return data


class TestHealth:
    async def test_health_ok_after_recent_pulse(self, api):
        from services import health as pulse

        client, _ = api
        pulse.record_ok()
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_health_503_when_telegram_stale(self, api, monkeypatch):
        import time

        from services import health as pulse

        client, _ = api
        stale = time.monotonic() - 10_000
        monkeypatch.setattr(pulse, "_last_ok", stale)
        monkeypatch.setattr(pulse, "_started", stale)
        resp = await client.get("/api/health")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False


class TestSubmitAuth:
    async def test_no_init_data_401(self, api):
        client, _ = api
        resp = await client.post("/api/invoice", data=_form())
        assert resp.status_code == 401

    async def test_fail_closed_403(self, api):
        client, _ = api  # whitelist пуст → закрыто
        resp = await client.post("/api/invoice", data=_form(), headers=_auth())
        assert resp.status_code == 403


class TestSubmitFlow:
    async def test_requisites_flow_writes_registry(self, api, monkeypatch):
        client, bot = api
        _allow(monkeypatch)
        resp = await client.post("/api/invoice", data=_form(), headers=_auth())
        assert resp.status_code == 200, resp.text
        request_id = resp.json()["request_id"]
        assert request_id.startswith("INV-")
        assert settings.registry_path.exists()
        bot.send_document.assert_awaited()  # подтверждение автору (PDF-подписью)

    async def test_file_flow_saves_invoice(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.post(
            "/api/invoice",
            data=_form(has_invoice="1", requisites=""),
            files={"file": ("счёт.pdf", b"%PDF-1.4 test", "application/pdf")},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        saved = list(settings.storage_path.glob("*.pdf"))
        assert any(not p.name.startswith("INV-") for p in saved)  # сам счёт сохранён

    async def test_precheck_endpoint(self, api, monkeypatch):
        from services import invoice_check

        client, _ = api
        _allow(monkeypatch)
        monkeypatch.setattr(
            invoice_check, "inspect_invoice_file", lambda *a, **k: ("⚠️ не счёт", "")
        )
        resp = await client.post(
            "/api/check-file",
            data={"amount": "125 000,50"},
            files={"file": ("cat.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["warning"] == "⚠️ не счёт"

        no_auth = await client.post(
            "/api/check-file",
            files={"file": ("cat.jpg", b"\xff\xd8\xff fake", "image/jpeg")},
        )
        assert no_auth.status_code == 401

    async def test_file_warning_returned_to_app(self, api, monkeypatch):
        from services import invoice_check

        client, _ = api
        _allow(monkeypatch)
        monkeypatch.setattr(
            invoice_check, "check_invoice_file", lambda *a, **k: "⚠️ тест: не счёт"
        )
        resp = await client.post(
            "/api/invoice",
            data=_form(has_invoice="1", requisites=""),
            files={"file": ("cat.jpg", b"\xff\xd8\xff fake-jpeg", "image/jpeg")},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["warning"] == "⚠️ тест: не счёт"

    async def test_comment_is_optional(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.post(
            "/api/invoice", data=_form(comment=""), headers=_auth()
        )
        assert resp.status_code == 200, resp.text

    async def test_auto_planned_date_is_server_side(self, api, monkeypatch):
        from openpyxl import load_workbook

        from bot.scheduling import auto_planned_date

        client, _ = api
        _allow(monkeypatch)
        resp = await client.post(
            "/api/invoice", data=_form(urgency="URGENT"), headers=_auth()
        )
        assert resp.status_code == 200
        ws = load_workbook(settings.registry_path).active
        assert ws.cell(2, 2).value == auto_planned_date(True).strftime("%d.%m.%Y")


class TestSubmitValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"amount": "мусор"},
            {"amount": "1.00,50"},
            {"planned_date": "вчера"},
            {"planned_date": "01.01.2020"},
            {"currency": "BTC"},
            {"urgency": "ASAP"},
            {"has_invoice": "2"},
            {"comment": "x" * 501},   # комментарий необязателен, но лимит остался
            {"article": ""},
            {"has_invoice": "1", "requisites": ""},  # файл обязателен, но не приложен
        ],
    )
    async def test_422(self, api, monkeypatch, overrides):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.post("/api/invoice", data=_form(**overrides), headers=_auth())
        assert resp.status_code == 422, resp.text


class TestDedupAndRateLimit:
    async def test_duplicate_409_then_force(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        assert (await client.post("/api/invoice", data=_form(), headers=_auth())).status_code == 200
        second = await client.post("/api/invoice", data=_form(), headers=_auth())
        assert second.status_code == 409
        assert second.json()["duplicate"] is True
        forced = await client.post(
            "/api/invoice", data=_form(force="1"), headers=_auth()
        )
        assert forced.status_code == 200

    async def test_rate_limit_429(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        for i in range(5):
            resp = await client.post(
                "/api/invoice",
                data=_form(counterparty=f"Контрагент {i}"),
                headers=_auth(),
            )
            assert resp.status_code == 200
        resp = await client.post(
            "/api/invoice", data=_form(counterparty="шестой"), headers=_auth()
        )
        assert resp.status_code == 429


class TestAdminEndpoints:
    async def test_settings_403_for_non_admin(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.get("/api/admin/settings", headers=_auth())
        assert resp.status_code == 403

    async def test_settings_with_backup_config(self, api, monkeypatch):
        client, _ = api
        _admins(monkeypatch, "42")
        resp = await client.get("/api/admin/settings", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["backup"]) == {"enabled", "time", "keep"}
        assert body["registry_url"] is None  # локальный режим — без ссылки

    async def test_settings_registry_url_in_google_mode(self, api, monkeypatch):
        client, _ = api
        _admins(monkeypatch, "42")
        monkeypatch.setattr(settings, "storage_backend", "google")
        monkeypatch.setattr(settings, "google_sheet_id", "SHEET123")
        resp = await client.get("/api/admin/settings", headers=_auth())
        assert resp.json()["registry_url"].endswith("/SHEET123")

    async def test_settings_drive_url_in_google_mode(self, api, monkeypatch):
        """Рядом с таблицей — папка Диска, куда складываются файлы счетов."""
        client, _ = api
        _admins(monkeypatch, "42")
        monkeypatch.setattr(settings, "storage_backend", "google")
        monkeypatch.setattr(settings, "google_drive_folder_id", "FOLDER123")
        resp = await client.get("/api/admin/settings", headers=_auth())
        assert resp.json()["drive_url"].endswith("/folders/FOLDER123")

    async def test_settings_drive_url_absent_without_folder(self, api, monkeypatch):
        """Папка не задана или хранилище локальное — ссылки нет, кнопка скрыта."""
        client, _ = api
        _admins(monkeypatch, "42")
        assert (await client.get("/api/admin/settings", headers=_auth())
                ).json()["drive_url"] is None
        monkeypatch.setattr(settings, "storage_backend", "google")
        monkeypatch.setattr(settings, "google_drive_folder_id", "")
        assert (await client.get("/api/admin/settings", headers=_auth())
                ).json()["drive_url"] is None

    async def test_backup_save_validation_and_roundtrip(self, api, monkeypatch):
        client, _ = api
        _admins(monkeypatch, "42")
        bad = await client.post(
            "/api/admin/backup",
            json={"action": "save", "enabled": True, "time": "25:99", "keep": "7"},
            headers=_auth(),
        )
        assert bad.status_code == 422
        ok = await client.post(
            "/api/admin/backup",
            json={"action": "save", "enabled": False, "time": "04:15", "keep": "3"},
            headers=_auth(),
        )
        assert ok.status_code == 200
        assert ok.json()["backup"] == {"enabled": False, "time": "04:15", "keep": 3}

    async def test_counterparties_after_submit(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        await client.post("/api/invoice", data=_form(), headers=_auth())
        resp = await client.get("/api/counterparties", headers=_auth())
        assert resp.status_code == 200
        names = [it["name"] for it in resp.json()["items"]]
        assert "ООО «Ромашка»" in names


class TestMyRequests:
    """«Мои заявки» через API: выдаются только свои, отзыв меняет статус."""

    async def test_lists_only_own_requests(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch, "42, 43")
        await client.post("/api/invoice", data=_form(), headers=_auth(42))
        await client.post(
            "/api/invoice", data=_form(counterparty="Чужой контрагент"), headers=_auth(43)
        )

        resp = await client.get("/api/my-requests", headers=_auth(42))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["counterparty"] == "ООО «Ромашка»"
        assert items[0]["status"] == "Новая"
        # Путь к файлу/ссылка наружу не отдаются — только признак.
        assert "file_url" not in items[0]

    async def test_requires_signed_init_data(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        assert (await client.get("/api/my-requests")).status_code == 401
        assert (
            await client.post("/api/my/withdraw", json={"request_id": "INV-1"})
        ).status_code == 401

    async def test_withdraw_marks_request_and_blocks_repeat(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]

        resp = await client.post(
            "/api/my/withdraw", json={"request_id": request_id}, headers=_auth()
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        items = (await client.get("/api/my-requests", headers=_auth())).json()["items"]
        assert items[0]["status"] == "Отозвана"

        # Повторный отзыв уже отозванной — отказ с понятным текстом.
        again = await client.post(
            "/api/my/withdraw", json={"request_id": request_id}, headers=_auth()
        )
        assert again.json()["ok"] is False

    async def test_cannot_withdraw_alien_request(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch, "42, 43")
        submit = await client.post("/api/invoice", data=_form(), headers=_auth(43))
        request_id = submit.json()["request_id"]

        resp = await client.post(
            "/api/my/withdraw", json={"request_id": request_id}, headers=_auth(42)
        )
        assert resp.json()["ok"] is False
        assert "только свою" in resp.json()["message"]

    async def test_bad_request_id_rejected(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.post(
            "/api/my/withdraw", json={"request_id": "'; DROP TABLE requests--"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    async def test_single_request_lookup_for_repeat(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]

        resp = await client.get(
            f"/api/my-requests?request_id={request_id}", headers=_auth()
        )
        items = resp.json()["items"]
        assert len(items) == 1
        # Для повтора важны реквизиты и признак «был ли счёт».
        assert items[0]["requisites"] == "ИНН 7707083893"
        assert items[0]["has_invoice"] is False

    async def test_counterparties_carry_requisites(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        await client.post("/api/invoice", data=_form(), headers=_auth())
        resp = await client.get("/api/counterparties", headers=_auth())
        item = resp.json()["items"][0]
        assert item["name"] == "ООО «Ромашка»"
        assert item["requisites"] == "ИНН 7707083893"


class TestFinancePanel:
    """Панель «Все заявки»: доступ только финансистам, фильтры, итоги."""

    def _financiers(self, monkeypatch, ids: str = "42") -> None:
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", ids)

    async def _submit(self, client, monkeypatch, **overrides):
        resp = await client.post(
            "/api/invoice", data=_form(**overrides), headers=_auth(42)
        )
        # Молчаливый отказ уводил расследование не туда: заявка не создавалась,
        # а падала проверка выборки строк на тридцать ниже.
        assert resp.status_code == 200, f"заявка не создана: {resp.text[:200]}"
        return resp

    async def test_outsider_gets_403(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "999")
        resp = await client.get("/api/finance/requests", headers=_auth(42))
        assert resp.status_code == 403

    async def test_access_flag_tells_ui_to_show_button(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "999")
        assert (await client.get("/api/finance/access", headers=_auth(42))).json() == {
            "ok": False
        }
        self._financiers(monkeypatch, "42")
        assert (await client.get("/api/finance/access", headers=_auth(42))).json() == {
            "ok": True
        }

    async def test_admin_without_finance_role_has_no_panel(self, api, monkeypatch):
        """Убрал себя из финансистов — панель пропадает, даже у админа."""
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "999")
        _admins(monkeypatch, "42")
        assert (await client.get("/api/finance/requests", headers=_auth(42))).status_code == 403
        assert (await client.get("/api/finance/access", headers=_auth(42))).json() == {
            "ok": False
        }

    async def test_shows_requests_of_all_authors(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch, "42, 43")
        self._financiers(monkeypatch, "42")
        await self._submit(client, monkeypatch)
        await client.post(
            "/api/invoice", data=_form(counterparty="Чужой контрагент"), headers=_auth(43)
        )

        data = (await client.get("/api/finance/requests", headers=_auth(42))).json()
        names = {it["counterparty"] for it in data["items"]}
        assert names == {"ООО «Ромашка»", "Чужой контрагент"}
        # Финансисту важно, кто подал.
        assert all(it["sender"] for it in data["items"])

    async def test_status_filter(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "42")
        first = await self._submit(client, monkeypatch)
        await self._submit(client, monkeypatch, counterparty="ООО «Вторая»")
        await client.post(
            "/api/my/withdraw",
            json={"request_id": first.json()["request_id"]},
            headers=_auth(42),
        )

        new_only = (await client.get(
            "/api/finance/requests?status=Новая", headers=_auth(42)
        )).json()
        assert new_only["total_found"] == 1
        assert new_only["items"][0]["counterparty"] == "ООО «Вторая»"
        assert new_only["shown"] == 1

        withdrawn = (await client.get(
            "/api/finance/requests?status=Отозвана", headers=_auth(42)
        )).json()
        assert withdrawn["total_found"] == 1

    async def test_query_matches_counterparty_and_article(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "42")
        await self._submit(client, monkeypatch)
        await self._submit(
            client, monkeypatch, counterparty="ИП Петров", article="Хостинг и ПО"
        )

        by_name = (await client.get(
            "/api/finance/requests?query=петров", headers=_auth(42)
        )).json()
        assert by_name["total_found"] == 1
        by_article = (await client.get(
            "/api/finance/requests?query=хостинг", headers=_auth(42)
        )).json()
        assert by_article["total_found"] == 1

    async def test_urgency_filter(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "42")
        await self._submit(client, monkeypatch)
        await self._submit(
            client, monkeypatch, urgency="URGENT", counterparty="ООО «Срочная»"
        )

        urgent = (await client.get(
            "/api/finance/requests?urgency=Срочно", headers=_auth(42)
        )).json()
        assert urgent["total_found"] == 1
        assert urgent["items"][0]["counterparty"] == "ООО «Срочная»"

    async def test_date_range_filter(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "42")
        # Даты считаем от сегодняшнего дня: зашитая «20.08.2026» однажды стала
        # вчерашней, валидатор отверг заявку, и тест начал падать сам по себе.
        target = date.today() + timedelta(days=5)
        await self._submit(
            client, monkeypatch, planned_date=target.strftime("%d.%m.%Y")
        )

        lo = (target - timedelta(days=2)).strftime("%d.%m.%Y")
        hi = (target + timedelta(days=2)).strftime("%d.%m.%Y")
        inside = (await client.get(
            f"/api/finance/requests?date_from={lo}&date_to={hi}",
            headers=_auth(42),
        )).json()
        assert inside["total_found"] == 1

        after = (target + timedelta(days=30)).strftime("%d.%m.%Y")
        outside = (await client.get(
            f"/api/finance/requests?date_from={after}", headers=_auth(42)
        )).json()
        assert outside["total_found"] == 0

    async def test_bad_date_is_rejected(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "42")
        resp = await client.get(
            "/api/finance/requests?date_from=позавчера", headers=_auth(42)
        )
        assert resp.status_code == 422

    async def test_requires_signed_init_data(self, api, monkeypatch):
        client, _ = api
        self._financiers(monkeypatch, "42")
        assert (await client.get("/api/finance/requests")).status_code == 401
        assert (await client.get("/api/finance/access")).status_code == 401


class TestDeleteEndpoint:
    async def test_author_deletes_own_withdrawn_request(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]

        # Пока «Новая» — нельзя.
        first = await client.post(
            "/api/requests/delete", json={"request_id": request_id}, headers=_auth()
        )
        assert first.json()["ok"] is False

        await client.post(
            "/api/my/withdraw", json={"request_id": request_id}, headers=_auth()
        )
        second = await client.post(
            "/api/requests/delete", json={"request_id": request_id}, headers=_auth()
        )
        assert second.json()["ok"] is True
        assert (await client.get("/api/my-requests", headers=_auth())).json()["items"] == []

    async def test_admin_deletes_any_request(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch, "42, 43")
        _admins(monkeypatch, "42")
        submit = await client.post("/api/invoice", data=_form(), headers=_auth(43))
        request_id = submit.json()["request_id"]

        resp = await client.post(
            "/api/requests/delete", json={"request_id": request_id}, headers=_auth(42)
        )
        assert resp.json()["ok"] is True

    async def test_stranger_cannot_delete(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch, "42, 43")
        submit = await client.post("/api/invoice", data=_form(), headers=_auth(43))
        request_id = submit.json()["request_id"]

        resp = await client.post(
            "/api/requests/delete", json={"request_id": request_id}, headers=_auth(42)
        )
        assert resp.json()["ok"] is False

    async def test_bad_id_and_unsigned(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        bad = await client.post(
            "/api/requests/delete", json={"request_id": "DROP TABLE"}, headers=_auth()
        )
        assert bad.status_code == 422
        assert (
            await client.post("/api/requests/delete", json={"request_id": "INV-1"})
        ).status_code == 401


class TestStaticPage:
    async def test_form_page_is_not_cached(self, api):
        """WebView Telegram держит страницу цепко: после деплоя пользователь
        неделями видел бы старую разметку и старый JS."""
        client, _ = api
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert "<title>" in resp.text or "id=\"form-view\"" in resp.text


class TestFinanceStatusEndpoint:
    """Смена статуса из панели — тот же сценарий, что кнопки в чате."""

    def _financiers(self, monkeypatch, ids: str = "42") -> None:
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", ids)

    async def test_financier_marks_paid(self, api, monkeypatch):
        client, bot = api
        _allow(monkeypatch)
        self._financiers(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]

        resp = await client.post(
            "/api/finance/status",
            json={"request_id": request_id, "key": "PAID"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        items = (await client.get("/api/my-requests", headers=_auth())).json()["items"]
        assert items[0]["status"] == "Оплачена"
        # Автор узнаёт о смене статуса.
        assert any(
            "Статус вашей заявки обновлён" in (c.kwargs.get("text") or "")
            for c in bot.send_message.await_args_list
        )

    async def test_reason_reaches_the_author_and_the_list(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]

        await client.post(
            "/api/finance/status",
            json={"request_id": request_id, "key": "REJECTED", "reason": "нет бюджета"},
            headers=_auth(),
        )
        items = (await client.get("/api/my-requests", headers=_auth())).json()["items"]
        assert items[0]["status"] == "Отклонена"
        assert items[0]["reason"] == "нет бюджета"

    async def test_outsider_cannot_change_status(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch, "999")
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        resp = await client.post(
            "/api/finance/status",
            json={"request_id": submit.json()["request_id"], "key": "PAID"},
            headers=_auth(),
        )
        assert resp.status_code == 403

    async def test_withdrawn_request_is_locked(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        request_id = submit.json()["request_id"]
        await client.post(
            "/api/my/withdraw", json={"request_id": request_id}, headers=_auth()
        )

        resp = await client.post(
            "/api/finance/status",
            json={"request_id": request_id, "key": "PAID"},
            headers=_auth(),
        )
        assert resp.json()["ok"] is False
        assert "отозвана" in resp.json()["message"].lower()

    async def test_bad_key_and_unsigned(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._financiers(monkeypatch)
        submit = await client.post("/api/invoice", data=_form(), headers=_auth())
        bad = await client.post(
            "/api/finance/status",
            json={"request_id": submit.json()["request_id"], "key": "APPROVED"},
            headers=_auth(),
        )
        assert bad.status_code == 422
        assert (
            await client.post("/api/finance/status", json={"request_id": "INV-1"})
        ).status_code == 401


class TestReminderSettings:
    """Общих настроек напоминаний больше нет: расписание у каждого своё.

    Ручка админа удалена вместе с карточкой — иначе осталась бы точка,
    которая молча меняет расписание всем сразу.
    """

    async def test_shared_endpoint_is_gone(self, api, monkeypatch):
        client, _ = api
        _admins(monkeypatch, "42")
        resp = await client.post(
            "/api/admin/reminders", json={"action": "run"}, headers=_auth()
        )
        # 404 или 405 — важно, что ручка больше ничего не делает.
        assert resp.status_code in (404, 405), resp.status_code


class TestAutofillBeta:
    """Бета: разбор счёта отдаётся форме как ПРЕДЛОЖЕНИЕ, не как действие."""

    INVOICE = (
        "Счет на оплату № 118 от 04.08.2026\n"
        "Поставщик (Исполнитель): ООО «Ромашка»\n"
        "ИНН 7707083893  КПП 773601001\n"
        "БИК 044525225\nР/с 40702810400000012345\n"
        "Всего к оплате: 174387.21\n"
    )

    def _text(self, monkeypatch, text: str = "") -> None:
        from services import invoice_check

        monkeypatch.setattr(
            invoice_check, "_extract_text", lambda c, f: (text or self.INVOICE, True)
        )

    async def test_precheck_returns_parsed_fields(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        self._text(monkeypatch)

        resp = await client.post(
            "/api/check-file",
            files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")},
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()["autofill"]
        assert data["amount"] == "174387.21"
        assert data["counterparty"] == "ООО «Ромашка»"
        assert data["inn"] == "7707083893"
        assert "Р/с 40702810400000012345" in data["requisites"]

    async def test_switched_off_returns_nothing(self, api, monkeypatch):
        """Выключили бету — форма ведёт себя ровно как раньше."""
        from services import runtime_settings as rs

        client, _ = api
        _allow(monkeypatch)
        self._text(monkeypatch)
        rs.set_autofill(False)

        resp = await client.post(
            "/api/check-file",
            files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")},
            headers=_auth(),
        )
        assert resp.json()["autofill"] == {}
        assert "warning" in resp.json()      # основная проверка не пострадала

    async def test_unreadable_file_gives_empty_autofill(self, api, monkeypatch):
        from services import invoice_check

        client, _ = api
        _allow(monkeypatch)
        monkeypatch.setattr(invoice_check, "_extract_text", lambda c, f: ("", False))

        resp = await client.post(
            "/api/check-file",
            files={"file": ("cat.jpg", b"\xff\xd8\xff", "image/jpeg")},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["autofill"] == {}

    async def test_toggle_requires_admin(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        _admins(monkeypatch, "999")
        assert (await client.post(
            "/api/admin/autofill", json={"enabled": False}, headers=_auth()
        )).status_code == 403

    async def test_admin_toggles_and_settings_report_it(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        _admins(monkeypatch, "42")

        off = await client.post(
            "/api/admin/autofill", json={"enabled": False}, headers=_auth()
        )
        assert off.json()["autofill"] is False
        settings_now = await client.get("/api/admin/settings", headers=_auth())
        assert settings_now.json()["autofill"] is False

        on = await client.post(
            "/api/admin/autofill", json={"enabled": True}, headers=_auth()
        )
        assert on.json()["autofill"] is True


class TestUsersRoster:
    """Список «кто пользуется ботом» и управление админами."""

    async def test_roster_marks_roles_and_access(self, api, monkeypatch):
        from services import runtime_settings as rs

        client, _ = api
        _admins(monkeypatch, "42")
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "7")
        rs.add_allowed(8)
        resp = await client.get("/api/admin/users", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        by_id = {u["id"]: u for u in body["users"]}
        assert by_id[7]["access"] == "env"        # запись из .env, но отозвать можно
        assert by_id[8]["access"] == "dynamic"    # добавлена из панели
        assert by_id[42]["admin"] is True
        assert by_id[42]["admin_source"] == "env"
        assert by_id[42]["access"] is None        # админ подаёт и без whitelist
        assert body["whitelist_empty"] is False

    async def test_roster_hides_the_bot_and_chats(self, api, monkeypatch):
        """В списке только люди: ни самого бота, ни групп-получателей."""
        from services import runtime_settings as rs
        from services import user_directory

        client, _ = api
        _admins(monkeypatch, "42")
        user_directory._cache = {"self": settings.bot_id, "vasya": 55}
        rs.add_financier("-1001234567890")     # группа финансистов, не человек
        ids = {u["id"] for u in
               (await client.get("/api/admin/users", headers=_auth())).json()["users"]}
        assert 55 in ids
        assert settings.bot_id not in ids
        assert -1001234567890 not in ids

    async def test_roster_reports_empty_whitelist(self, api, monkeypatch):
        """Пустой whitelist — подача закрыта всем, кроме админов."""
        client, _ = api
        _admins(monkeypatch, "42")
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "")
        body = (await client.get("/api/admin/users", headers=_auth())).json()
        assert body["whitelist_empty"] is True
        assert all(u["access"] is None for u in body["users"])

    async def test_roster_requires_admin(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.get("/api/admin/users", headers=_auth())
        assert resp.status_code == 403

    async def test_env_access_can_be_revoked(self, api, monkeypatch):
        """Отзыв записи из .env: человеку снова придётся просить доступ."""
        from bot.access import is_allowed

        client, _ = api
        _admins(monkeypatch, "42")
        settings.__dict__.pop("allowed_user_ids", None)
        monkeypatch.setattr(settings, "allowed_user_ids_raw", "7")
        assert is_allowed(7)
        resp = await client.post(
            "/api/admin/allowed", json={"action": "remove", "entry": "7"}, headers=_auth()
        )
        assert resp.json()["ok"] is True
        assert not is_allowed(7)
        # Возвращается обычным «добавить».
        await client.post(
            "/api/admin/allowed", json={"action": "add", "entry": "7"}, headers=_auth()
        )
        assert is_allowed(7)

    async def test_admin_can_be_appointed_and_removed(self, api, monkeypatch):
        from bot.access import is_admin

        client, _ = api
        _admins(monkeypatch, "42")
        add = await client.post(
            "/api/admin/admins", json={"action": "add", "entry": "77"}, headers=_auth()
        )
        assert add.json()["ok"] is True
        assert is_admin(77)
        drop = await client.post(
            "/api/admin/admins", json={"action": "remove", "entry": "77"}, headers=_auth()
        )
        assert drop.json()["ok"] is True
        assert not is_admin(77)

    async def test_env_admin_cannot_be_demoted(self, api, monkeypatch):
        """Владелец из .env остаётся владельцем — даже для другого админа."""
        from bot.access import is_admin

        client, _ = api
        _admins(monkeypatch, "42")
        await client.post(
            "/api/admin/admins", json={"action": "add", "entry": "77"}, headers=_auth()
        )
        resp = await client.post(
            "/api/admin/admins", json={"action": "remove", "entry": "42"}, headers=_auth()
        )
        assert resp.status_code == 409
        assert ".env" in resp.json()["detail"]
        assert is_admin(42)

    async def test_admins_endpoint_requires_admin(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        resp = await client.post(
            "/api/admin/admins", json={"action": "add", "entry": "1"}, headers=_auth()
        )
        assert resp.status_code == 403


class TestAccessRequests:
    """Запрос доступа сотрудником и решение админа."""

    async def test_state_reports_denied_and_pending(self, api, monkeypatch):
        client, _ = api
        _admins(monkeypatch, "1")
        first = (await client.get("/api/access", headers=_auth())).json()
        assert first == {"allowed": False, "financier": False, "admin": False,
                         "pending": False, "has_admins": True}
        await client.post("/api/access/request", headers=_auth())
        assert (await client.get("/api/access", headers=_auth())).json()["pending"] is True

    async def test_request_notifies_every_admin_once(self, api, monkeypatch):
        client, bot = api
        _admins(monkeypatch, "1,2")
        resp = await client.post("/api/access/request", headers=_auth())
        assert resp.status_code == 200
        assert bot.send_message.await_count == 2
        # Повторное нажатие админов больше не беспокоит.
        again = await client.post("/api/access/request", headers=_auth())
        assert "уже отправлена" in again.json()["message"]
        assert bot.send_message.await_count == 2

    async def test_request_without_admins_says_so(self, api, monkeypatch):
        client, bot = api
        _admins(monkeypatch, "")
        resp = await client.post("/api/access/request", headers=_auth())
        assert "не задан ни один админ" in resp.json()["message"]
        bot.send_message.assert_not_awaited()

    async def test_request_needs_signature(self, api):
        client, _ = api
        assert (await client.post("/api/access/request")).status_code == 401
        assert (await client.get("/api/access")).status_code == 401

    async def test_allowed_user_gets_no_request(self, api, monkeypatch):
        client, bot = api
        _allow(monkeypatch)
        resp = await client.post("/api/access/request", headers=_auth())
        assert resp.json()["ok"] is False
        bot.send_message.assert_not_awaited()

    async def test_approval_opens_access_and_answers_the_author(self, api, monkeypatch):
        from bot.access import is_allowed
        from services.access_requests import resolve_access

        client, bot = api
        _admins(monkeypatch, "1")
        await client.post("/api/access/request", headers=_auth())
        assert not is_allowed(42)
        note = await resolve_access(bot, 42, True, actor_id=1, actor_name="@boss")
        assert "Доступ открыт" in note
        assert is_allowed(42)
        # Заявка снята — повторная просьба снова дойдёт до админов.
        assert (await client.get("/api/access", headers=_auth())).json()["pending"] is False
        assert any("Доступ открыт" in str(c) for c in bot.send_message.await_args_list)

    async def test_rejection_leaves_access_closed(self, api, monkeypatch):
        from bot.access import is_allowed
        from services.access_requests import resolve_access

        client, bot = api
        _admins(monkeypatch, "1")
        await client.post("/api/access/request", headers=_auth())
        await resolve_access(bot, 42, False, actor_id=1, actor_name="@boss")
        assert not is_allowed(42)
        assert (await client.get("/api/access", headers=_auth())).json()["pending"] is False

    async def test_request_remembers_the_username_for_the_whitelist(self, api, monkeypatch):
        """После выдачи доступа в настройках виден @ник, а не «id 42».

        Whitelist хранит числовые id (ник можно сменить), а показывает
        справочник; из Mini App апдейта в чат нет, поэтому ник туда попадает
        только отсюда.
        """
        from services.access_requests import resolve_access
        from services.user_directory import username_for

        client, bot = api
        _admins(monkeypatch, "1")
        assert username_for(42) is None
        await client.post("/api/access/request", headers=_auth())
        assert username_for(42) == "@tester"

        note = await resolve_access(bot, 42, True, actor_id=1, actor_name="@boss")
        assert "@tester" in note, "решение админа тоже должно называть человека по нику"
        body = (await client.get("/api/admin/settings", headers=_auth(1))).json()
        rows = [u for u in body["allowed"] if u["id"] == 42]
        assert rows and rows[0]["username"] == "@tester"


class TestPersonalReminders:
    """Каждый получатель настраивает напоминания себе, а не всем сразу."""

    async def test_only_recipients_may_read_and_save(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)          # обычный сотрудник: напоминать нечего
        assert (await client.get("/api/reminders/me", headers=_auth())).status_code == 403
        assert (await client.post("/api/reminders/me", json={},
                                  headers=_auth())).status_code == 403

    async def test_financier_sets_own_schedule(self, api, monkeypatch):
        from services import runtime_settings as rs

        client, _ = api
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "42")

        first = (await client.get("/api/reminders/me", headers=_auth())).json()
        assert first["custom"] is False, "пока ничего не меняли — общие настройки"

        saved = await client.post("/api/reminders/me", json={
            "enabled": True, "time": "07:15", "days_before": "3",
            "overdue_enabled": False,
        }, headers=_auth())
        assert saved.status_code == 200
        cfg = saved.json()["reminders"]
        assert (cfg["time"], cfg["days_before"], cfg["overdue_enabled"]) == ("07:15", 3, False)
        assert cfg["custom"] is True
        # Общие настройки при этом не тронуты — это и есть смысл личных.
        assert rs.reminders_config()["time"] != "07:15"

        back = await client.post("/api/reminders/me", json={"action": "reset"},
                                 headers=_auth())
        assert back.json()["reminders"]["custom"] is False

    async def test_personal_time_is_validated(self, api, monkeypatch):
        client, _ = api
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "42")
        bad_time = await client.post("/api/reminders/me", json={"time": "25:99"},
                                     headers=_auth())
        assert bad_time.status_code == 422
        bad_days = await client.post("/api/reminders/me", json={"days_before": "99"},
                                     headers=_auth())
        assert bad_days.status_code == 422

class TestHelpCommand:
    """/help показывает только то, что доступно этому человеку."""

    async def _help(self, bot, user_id: int) -> str:
        from bot.commands import build_help

        return await build_help(bot, user_id)

    async def test_plain_employee_sees_common_commands_only(self, api, monkeypatch):
        _, bot = api
        _allow(monkeypatch)
        text = await self._help(bot, 42)
        assert "/invoice" in text and "/my" in text and "/myid" in text
        assert "/allow" not in text, "админские команды не для всех"
        assert "Финансисту" not in text

    async def test_financier_sees_their_block(self, api, monkeypatch):
        _, bot = api
        _allow(monkeypatch)
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "42")
        text = await self._help(bot, 42)
        assert "Финансисту" in text
        assert "/allow" not in text

    async def test_admin_sees_everything(self, api, monkeypatch):
        _, bot = api
        _admins(monkeypatch, "42")
        text = await self._help(bot, 42)
        assert "Админу" in text
        for command in ("/allow", "/deny", "/fin_add", "/export", "/audit", "/backup"):
            assert command in text, command

    async def test_without_access_help_says_how_to_get_it(self, api):
        _, bot = api
        text = await self._help(bot, 42)     # whitelist пуст → доступа нет
        assert "Запросить доступ" in text


class TestOverdueFilter:
    """Фильтр «Просрочены»: срок прошёл, а заявка всё ещё ждёт оплаты."""

    async def _seed(self, request_id: str, days: int):
        from datetime import date, timedelta

        from services import storage
        from tests.conftest import make_request

        await storage.append_invoice(make_request(
            request_id=request_id, planned_date=date.today() + timedelta(days=days)))

    async def test_overdue_is_a_state_not_a_status(self, api, monkeypatch):
        client, _ = api
        _allow(monkeypatch)
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "42")
        await self._seed("INV-20260804-000001-0001", -3)   # срок прошёл
        await self._seed("INV-20260804-000002-0002", +5)   # ещё впереди

        resp = await client.get("/api/finance/requests?status=__overdue__", headers=_auth())
        assert resp.status_code == 200
        assert [r["id"] for r in resp.json()["items"]] == ["INV-20260804-000001-0001"]

    async def test_paid_request_is_never_overdue(self, api, monkeypatch):
        from services import storage

        client, _ = api
        _allow(monkeypatch)
        settings.__dict__.pop("finance_recipients", None)
        monkeypatch.setattr(settings, "finance_chat_ids_raw", "42")
        await self._seed("INV-20260804-000003-0003", -3)
        await storage.set_request_status("INV-20260804-000003-0003", "Оплачена")
        resp = await client.get("/api/finance/requests?status=__overdue__", headers=_auth())
        assert resp.json()["items"] == []
