"""Смена канала до Telegram: когда уходим на перезапуск, а когда терпим.

Живой подмены клиента в PTB нет — канал вшит в Bot при сборке. «Переключиться»
значит выйти и дать контейнеру подняться заново: resolve_proxy переберёт
кандидатов и возьмёт первый отвечающий (замер на боевом: 4 секунды).
"""
from __future__ import annotations

import time

import pytest

from config import settings
from services import failover


@pytest.fixture(autouse=True)
def clean_marks(tmp_paths, monkeypatch):
    """Своя база на тест и два кандидата в PROXY_URL по умолчанию."""
    monkeypatch.setattr(
        settings, "proxy_url", "socks5://paid:1080,socks5://warp:1080", raising=False
    )
    settings.__dict__.pop("proxy_urls", None)
    failover.mark_switch_sync(0.0)
    yield
    settings.__dict__.pop("proxy_urls", None)


def test_single_channel_never_restarts(monkeypatch):
    """Один адрес в PROXY_URL — перезапуск ничего не изменит, значит не нужен."""
    monkeypatch.setattr(settings, "proxy_url", "socks5://warp:1080", raising=False)
    settings.__dict__.pop("proxy_urls", None)
    ok, why = failover.should_switch(10_000.0)
    assert ok is False
    assert "запасного канала нет" in why


def test_short_blip_is_tolerated():
    """WARP переподключает туннель десятки раз в сутки — это не повод выходить."""
    ok, why = failover.should_switch(failover.SWITCH_AFTER - 1)
    assert ok is False
    assert "порог" in why


def test_long_silence_switches():
    ok, why = failover.should_switch(failover.SWITCH_AFTER + 1)
    assert ok is True, why


def test_cooldown_survives_the_restart():
    """Отметка лежит в SQLite, а не в памяти: её и обнуляет перезапуск.

    Без диска бот уходил бы в круг перезапусков ровно тогда, когда лежит не
    прокси, а Telegram или сеть целиком, — то есть когда помочь нечем.
    """
    now = time.time()
    failover.mark_switch_sync(now - 60)          # переключались минуту назад
    ok, why = failover.should_switch(10_000.0, now=now)
    assert ok is False
    assert "ждём часа" in why

    # Час прошёл — можно снова.
    ok, _ = failover.should_switch(10_000.0, now=now + failover.COOLDOWN + 1)
    assert ok is True


async def test_switch_marks_before_leaving(monkeypatch):
    """Отметка ставится ДО выхода: после него писать её будет некому."""
    order: list[str] = []
    monkeypatch.setattr(failover, "request_restart", lambda: order.append("exit"))
    real_mark = failover.mark_switch_sync
    monkeypatch.setattr(
        failover, "mark_switch_sync",
        lambda *a, **kw: (order.append("mark"), real_mark(*a, **kw))[1],
    )
    assert await failover.maybe_switch(10_000.0) is True
    assert order == ["mark", "exit"], f"порядок нарушен: {order}"


async def test_no_switch_no_exit(monkeypatch):
    """Порог не пройден — процесс не трогаем."""
    called: list[str] = []
    monkeypatch.setattr(failover, "request_restart", lambda: called.append("exit"))
    assert await failover.maybe_switch(1.0) is False
    assert called == []


async def test_pulse_asks_failover_and_stops_the_tick(monkeypatch):
    """Пульс отдаёт решение failover и на положительном ответе такт прекращает.

    Иначе он следом попытался бы отправить алерт — по каналу, который как раз
    и молчит, — и задержал бы выход.
    """
    from unittest.mock import AsyncMock, MagicMock

    from services import health

    bot = MagicMock()
    bot.get_me = AsyncMock(side_effect=ConnectionError("proxy down"))
    alerted: list[str] = []
    monkeypatch.setattr(health, "_safe_alert",
                        AsyncMock(side_effect=lambda *a, **kw: alerted.append("alert")))
    monkeypatch.setattr(health.failover, "maybe_switch", AsyncMock(return_value=True))

    assert await health.probe_once(bot, healthy=True) is False
    assert alerted == [], "пульс полез уведомлять, хотя процесс уже уходит"
