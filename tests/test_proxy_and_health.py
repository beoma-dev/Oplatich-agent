"""Фейловер прокси (roadmap 1.5) и пульс Telegram для /api/health (1.4)."""
from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import NetworkError, TimedOut

import services.health as health
import services.proxy as proxy_mod
from config import settings


# ---------------------------------------------------------------------------
# pick_working_proxy: фейковый Bot вместо сети
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, proxy: str | None = None, **_kwargs):
        self.proxy = proxy


class _FakeBot:
    alive: set[str] = set()

    def __init__(self, _token: str, request: _FakeRequest):
        self._proxy = request.proxy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_me(self):
        if self._proxy not in self.alive:
            raise ConnectionError("proxy down")


@pytest.fixture()
def fake_bot(monkeypatch):
    monkeypatch.setattr(proxy_mod, "Bot", _FakeBot)
    monkeypatch.setattr(proxy_mod, "HTTPXRequest", _FakeRequest)
    _FakeBot.alive = set()
    return _FakeBot


CANDIDATES = ["socks5://warp:1080", "socks5://backup:1081"]


async def test_first_dead_second_wins(fake_bot):
    fake_bot.alive = {CANDIDATES[1]}
    assert await proxy_mod.pick_working_proxy("t", CANDIDATES) == CANDIDATES[1]


async def test_first_alive_short_circuit(fake_bot):
    fake_bot.alive = set(CANDIDATES)
    assert await proxy_mod.pick_working_proxy("t", CANDIDATES) == CANDIDATES[0]


async def test_all_dead_returns_none(fake_bot):
    assert await proxy_mod.pick_working_proxy("t", CANDIDATES) is None


def test_masked_hides_credentials():
    assert proxy_mod.masked("socks5://user:pass@host:1080") == "host:1080"
    assert proxy_mod.masked("socks5://host:1080") == "socks5://host:1080"


# ---------------------------------------------------------------------------
# resolve_proxy: единственный кандидат — без проверки, все мертвы — первый
# ---------------------------------------------------------------------------
def _set_proxy_url(monkeypatch, raw: str) -> None:
    settings.__dict__.pop("proxy_urls", None)
    monkeypatch.setattr(settings, "proxy_url", raw)


@pytest.fixture()
def _proxy_urls_cache_cleanup():
    yield
    settings.__dict__.pop("proxy_urls", None)


async def test_resolve_single_skips_probe(monkeypatch, _proxy_urls_cache_cleanup):
    import main as main_mod

    _set_proxy_url(monkeypatch, "socks5://only:1080")

    async def _boom(*_a, **_k):
        raise AssertionError("единственный прокси не должен проверяться")

    monkeypatch.setattr(main_mod, "pick_working_proxy", _boom)
    assert await main_mod.resolve_proxy() == "socks5://only:1080"


async def test_resolve_all_dead_falls_back_to_first(monkeypatch, _proxy_urls_cache_cleanup):
    import main as main_mod

    _set_proxy_url(monkeypatch, ", ".join(CANDIDATES))

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "pick_working_proxy", _none)
    assert await main_mod.resolve_proxy() == CANDIDATES[0]


async def test_resolve_empty_means_direct(monkeypatch, _proxy_urls_cache_cleanup):
    import main as main_mod

    _set_proxy_url(monkeypatch, "")
    assert await main_mod.resolve_proxy() == ""


# ---------------------------------------------------------------------------
# Пульс Telegram
# ---------------------------------------------------------------------------
async def test_probe_loop_records_success(monkeypatch):
    monkeypatch.setattr(health, "_last_ok", None)
    monkeypatch.setattr(health, "PROBE_INTERVAL", 0.01)
    bot = MagicMock()
    bot.get_me = AsyncMock()
    task = asyncio.create_task(health.probe_loop(bot))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert health.last_ok_age() is not None
    assert health.telegram_alive()


async def test_probe_loop_survives_errors(monkeypatch):
    monkeypatch.setattr(health, "_last_ok", None)
    monkeypatch.setattr(health, "PROBE_INTERVAL", 0.01)
    bot = MagicMock()
    bot.get_me = AsyncMock(side_effect=ConnectionError("нет сети"))
    task = asyncio.create_task(health.probe_loop(bot))
    await asyncio.sleep(0.05)
    assert not task.done()  # цикл не упал от ошибок сети
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert health.last_ok_age() is None


def test_alive_grace_then_stale(monkeypatch):
    # До первого пульса: свежий процесс здоров, старый — нет.
    monkeypatch.setattr(health, "_last_ok", None)
    monkeypatch.setattr(health, "_started", time.monotonic())
    assert health.telegram_alive()
    monkeypatch.setattr(health, "_started", time.monotonic() - 10_000)
    assert not health.telegram_alive()
    # После пульса решает возраст последнего успеха.
    monkeypatch.setattr(health, "_last_ok", time.monotonic())
    assert health.telegram_alive()
    monkeypatch.setattr(health, "_last_ok", time.monotonic() - 10_000)
    assert not health.telegram_alive()


class TestTelegramPin:
    """Проверка прибитого адреса Telegram.

    Проверяем ДОСТИЖИМОСТЬ, а не совпадение с DNS: 25.08.2026 выяснилось,
    что через сессию WARP адрес из DNS не отвечал, а другой адрес Telegram
    отвечал. Пин выбирается по тому, докуда WARP доходит, поэтому сравнение
    с DNS давало бы ложную тревогу каждый день.
    """

    def test_no_pin_is_not_an_error(self, monkeypatch):
        from services import dns_pin

        monkeypatch.setattr(settings, "telegram_pinned_ip", "")
        ok, message = dns_pin.check()
        assert ok and "не используется" in message

    def test_reachable_pin_is_fine_even_if_dns_disagrees(self, monkeypatch):
        from services import dns_pin

        monkeypatch.setattr(settings, "telegram_pinned_ip", "149.154.167.220")
        monkeypatch.setattr(dns_pin, "reachable", lambda: True)
        monkeypatch.setattr(dns_pin, "resolve_v4", lambda *_a: ["149.154.166.110"])
        ok, message = dns_pin.check()
        assert ok, "расхождение с DNS — не повод для тревоги"
        assert "149.154.166.110" in message, "но знать о нём полезно"

    def test_unreachable_pin_is_a_failure_with_instructions(self, monkeypatch):
        from services import dns_pin

        monkeypatch.setattr(settings, "telegram_pinned_ip", "149.154.167.220")
        monkeypatch.setattr(dns_pin, "reachable", lambda: False)
        monkeypatch.setattr(dns_pin, "resolve_v4", lambda *_a: ["149.154.166.110"])
        ok, message = dns_pin.check()
        assert not ok
        assert "не отвечает" in message
        assert "extra_hosts" in message and "TELEGRAM_PINNED_IP" in message


class TestRetryingTransport:
    """Повтор на уровне транспорта: его получают ВСЕ вызовы Bot API.

    Поставлен туда, а не на места вызова, потому что мест двадцать пять и
    половину забудешь: правка сообщения в диалоге, «Мои заявки», решение по
    доступу, отзыв заявки. Граница повтора — не «сетевая ошибка вообще»,
    а «запрос точно не дошёл».
    """

    async def _run(self, monkeypatch, side_effect):
        from telegram.request import HTTPXRequest

        from services import proxy

        monkeypatch.setattr(proxy.asyncio, "sleep", AsyncMock())
        calls = []

        async def fake(self, *a, **kw):
            calls.append(1)
            result = side_effect(len(calls))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(HTTPXRequest, "do_request", fake)
        req = proxy.RetryingRequest()
        return req, calls

    async def test_connection_failure_is_retried(self, monkeypatch):
        """ProxyError — соединение не встало, запрос не дошёл: повторяем."""
        req, calls = await self._run(
            monkeypatch,
            lambda n: NetworkError("httpx.ProxyError: Host unreachable") if n == 1 else (200, b"ok"),
        )
        assert await req.do_request() == (200, b"ok")
        assert len(calls) == 2

    async def test_timeout_is_NOT_retried(self, monkeypatch):
        """TimedOut — ответа не дождались, но запрос мог быть исполнен.

        Повтор создал бы вторую карточку, второе сообщение, второй документ.
        Молчание здесь дешевле дубля.
        """
        req, calls = await self._run(monkeypatch, lambda n: TimedOut())
        with pytest.raises(TimedOut):
            await req.do_request()
        assert len(calls) == 1, "таймаут повторили — риск дубля"

    async def test_gives_up_after_the_budget(self, monkeypatch):
        from services import proxy

        req, calls = await self._run(
            monkeypatch, lambda n: NetworkError("httpx.ConnectError: refused")
        )
        with pytest.raises(NetworkError):
            await req.do_request()
        assert len(calls) == len(proxy.API_RETRY_PAUSES) + 1

    def test_polling_client_keeps_ptb_defaults(self):
        """Подмена клиента не должна незаметно менять long polling."""
        from services import proxy

        api, polling = proxy.build_requests("socks5://warp:1080")
        assert isinstance(api, proxy.RetryingRequest)
        assert isinstance(polling, proxy.RetryingRequest)
        # У PTB пул опроса — 1, у обычных вызовов — 256. Сверяем с тем, что
        # реально ушло в httpx: подмена клиента не должна менять long polling.
        def pool_size(req):
            return req._client._transport._pool._max_connections

        assert pool_size(polling) == 1
        assert pool_size(api) == 256
