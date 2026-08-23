"""Две заявки в один момент: не теряются, не сливаются, не двоятся.

Область, которую тесты раньше не трогали вовсе. Проверяется настоящий путь
через ASGI: реестр, xlsx-зеркало, дедуп и нумерация строк — всё живое,
мокнут только Telegram.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import api.routes as routes_mod
import bot.access as access
from api.server import build_api
from config import settings
from services import storage
from tests.test_api_integration import _form, _make_bot
from tests.test_auth import _signed_init_data


def _auth(user_id: int) -> dict:
    return {
        "X-Telegram-Init-Data": _signed_init_data(
            user={"id": user_id, "first_name": "Тест", "username": f"u{user_id}"}
        )
    }


@pytest.fixture()
async def api(tmp_paths, monkeypatch):
    monkeypatch.setattr(routes_mod, "_rate", {})
    monkeypatch.setattr(routes_mod, "_my_rate", {})
    monkeypatch.setattr(routes_mod, "_check_rate", {})
    monkeypatch.setattr(access, "_admin_cache", {})
    settings.__dict__.pop("allowed_user_ids", None)
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "41, 42, 43")
    bot = _make_bot()
    transport = httpx.ASGITransport(app=build_api(bot))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, bot


async def _mirror_rows() -> int:
    import openpyxl

    path = storage.registry_export_path()
    if path is None or not path.exists():
        return 0
    ws = openpyxl.load_workbook(path, read_only=True).active
    return sum(
        1 for r in ws.iter_rows(min_row=2, values_only=True)
        if any(v not in (None, "") for v in r)
    )


class TestSimultaneousSubmissions:
    async def test_two_different_requests_both_survive(self, api):
        """Обе заявки на месте, номера строк разные, зеркало не потеряло ни одной."""
        client, _bot = api
        first, second = await asyncio.gather(
            client.post("/api/invoice", data=_form(counterparty="ООО «Первый»"),
                        headers=_auth(41)),
            client.post("/api/invoice", data=_form(counterparty="ООО «Второй»"),
                        headers=_auth(42)),
        )
        assert first.status_code == 200 and second.status_code == 200
        got = {first.json()["request_id"], second.json()["request_id"]}
        assert len(got) == 2, f"обеим заявкам выдали один ID: {got}"

        ids = await storage.all_request_ids()
        assert set(ids) == got, "в реестре не те заявки, что вернул API"
        assert len(ids) == 2, "в реестре не две заявки"
        assert await _mirror_rows() == 2, "зеркало потеряло заявку при одновременной записи"

    async def test_identical_requests_at_the_same_instant(self, api):
        """Один и тот же счёт, отправленный дважды одновременно.

        Дедуп проверяет отпечаток ДО записи, поэтому у двух одновременных
        подач есть окно, в которое обе проходят проверку. Тест фиксирует
        фактическое поведение: если оно изменится, мы об этом узнаем.
        """
        client, _bot = api
        payload = _form(counterparty="ООО «Одинаковый»", amount="7 777,00")
        first, second = await asyncio.gather(
            client.post("/api/invoice", data=dict(payload), headers=_auth(41)),
            client.post("/api/invoice", data=dict(payload), headers=_auth(41)),
        )
        codes = sorted([first.status_code, second.status_code])
        accepted = await storage.all_request_ids()
        # Либо дедуп успел (409 на второй), либо обе прошли в окно гонки.
        print(f"\n  фактическое поведение при одновременных дублях: {codes}")
        assert codes in ([200, 200], [200, 409]), codes
        assert len(accepted) == len(
            [c for c in codes if c == 200]
        ), "реестр разошёлся с ответами API"
        assert await _mirror_rows() == len(accepted), "зеркало разошлось с реестром"

    async def test_five_at_once_all_land(self, api):
        """Пять подач сразу: пять записей в реестре и пять в зеркале.

        ID собирается из времени и суффикса `(telegram_id + микросекунды) % 10000`,
        а в SQLite на него стоит UNIQUE. Столкновение суффиксов у одного
        сотрудника в одну секунду возможно примерно раз на десять тысяч — и
        тогда заявка честно не принимается (ошибка), а не двоится в реестре.
        Тест держит именно это: сколько ответов «принято», столько и записей.
        """
        client, _bot = api
        results = await asyncio.gather(*[
            client.post(
                "/api/invoice",
                data=_form(counterparty=f"ООО «Номер {i}»", amount=f"{1000 + i},00"),
                headers=_auth(41 + i % 3),
            )
            for i in range(5)
        ])
        ok = [r for r in results if r.status_code == 200]
        assert len(ok) == 5, [r.status_code for r in results]
        assert len({r.json()["request_id"] for r in ok}) == 5, "ID повторились"
        assert len(await storage.all_request_ids()) == 5
        assert await _mirror_rows() == 5
