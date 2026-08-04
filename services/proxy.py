"""Выбор рабочего прокси для api.telegram.org («план Б», roadmap 1.5).

PROXY_URL допускает несколько адресов через запятую. При старте бот пробует
их по порядку коротким getMe и подключается через первый отвечающий — смерть
основного прокси (например, WARP) перестаёт быть единой точкой отказа:
достаточно перезапустить контейнер, и бот уйдёт на запасной.
"""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0


def masked(url: str) -> str:
    """Адрес прокси без user:pass — для логов."""
    return url.rsplit("@", 1)[-1]


async def pick_working_proxy(token: str, candidates: list[str]) -> str | None:
    """Первый прокси из списка, через который отвечает api.telegram.org.

    None — не ответил ни один (решение, как стартовать, за вызывающим).
    """
    for url in candidates:
        bot = Bot(
            token,
            request=HTTPXRequest(
                proxy=url,
                connect_timeout=PROBE_TIMEOUT,
                read_timeout=PROBE_TIMEOUT,
            ),
        )
        try:
            async with bot:
                await bot.get_me()
        except Exception as exc:  # noqa: BLE001 — прокси мёртв, пробуем следующий
            log.warning(
                "Прокси %s не отвечает (%s) — пробую следующий",
                masked(url), type(exc).__name__,
            )
            continue
        log.info("Выбран рабочий прокси: %s", masked(url))
        return url
    return None
