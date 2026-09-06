"""Выбор рабочего прокси для api.telegram.org («план Б», roadmap 1.5).

PROXY_URL допускает несколько адресов через запятую. При старте бот пробует
их по порядку коротким getMe и подключается через первый отвечающий — смерть
основного прокси (например, WARP) перестаёт быть единой точкой отказа:
достаточно перезапустить контейнер, и бот уйдёт на запасной.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import NetworkError, TimedOut
from telegram.request import BaseRequest, HTTPXRequest

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0

# Канал, через который бот РЕАЛЬНО ходит в Telegram: выбирается при старте из
# списка кандидатов, и знать его должен не только main. Раньше желающие брали
# settings.proxy_url — СЫРУЮ строку из .env, а в ней может быть несколько
# адресов через запятую. Такую строку httpx принимает за один адрес и падает:
# суточная проверка пина от этого делала ложный вывод «бот замолчит целиком».
_active: str = ""


def set_active(url: str) -> None:
    """Запоминает выбранный канал. Зовётся один раз при сборке приложения."""
    global _active
    _active = url


def active() -> str:
    """Выбранный канал или "" — прямое подключение (и до старта тоже "")."""
    return _active

# Паузы между попытками вызова Bot API. Короткие: человек ждёт ответа на
# нажатие кнопки, и лучше показать ошибку, чем держать его пять секунд.
API_RETRY_PAUSES: tuple[float, ...] = (0.5, 1.5)


class RetryingRequest(HTTPXRequest):
    """Клиент Bot API, повторяющий вызовы, которые до Telegram не дошли.

    Повтор стоит здесь, а не на местах вызова, по простой причине: мест
    двадцать пять, и половину из них забудешь. Через транспорт проходит всё
    — правка сообщения в диалоге, список «Моих заявок», решение по доступу,
    отзыв заявки, — включая то, о чём никто не подумал.

    Повторяем `NetworkError`, но НЕ `TimedOut`, и это принципиально.
    NetworkError у PTB — это httpx-ошибки уровня соединения (`ProxyError`,
    `ConnectError`): запрос до Telegram НЕ дошёл, повторить его безопасно.
    `TimedOut` означает, что ответа не дождались, — а запрос мог быть
    доставлен и исполнен. Повтор такого создал бы вторую карточку, второе
    сообщение, второй документ. Молчание здесь дешевле дубля.

    Прикладной повтор (services/tg_retry) при этом остаётся: он покрывает
    и таймауты там, где потеря дороже дубля, — карточка финансисту.
    """

    async def do_request(self, *args: object, **kwargs: object):  # type: ignore[override]
        for attempt, pause in enumerate((*API_RETRY_PAUSES, None), start=1):
            try:
                return await super().do_request(*args, **kwargs)  # type: ignore[arg-type]
            except TimedOut:
                # Ответа не дождались — запрос мог дойти. Не повторяем.
                raise
            except NetworkError as err:
                if pause is None:
                    raise
                log.warning(
                    "Bot API: соединение не установилось (%s), попытка %s из %s через %.1f с",
                    err.__class__.__name__, attempt, len(API_RETRY_PAUSES) + 1, pause,
                )
                await asyncio.sleep(pause)
        return None


def build_requests(proxy_url: str) -> tuple[BaseRequest, BaseRequest]:
    """Пара клиентов для PTB: обычные вызовы и long polling.

    Значения повторяют умолчания ApplicationBuilder (пул 256 против 1 у
    опроса, остальные таймауты — свои у HTTPXRequest), иначе подмена клиента
    незаметно поменяла бы поведение long polling.
    """
    proxy = proxy_url or None
    return (
        RetryingRequest(connection_pool_size=256, proxy=proxy),
        RetryingRequest(connection_pool_size=1, proxy=proxy),
    )


def masked(url: str) -> str:
    """Адрес прокси без user:pass — для логов."""
    return url.rsplit("@", 1)[-1]


async def pick_working_proxy(token: str, candidates: list[str]) -> str | None:
    """Первый прокси из списка, через который отвечает api.telegram.org.

    None — не ответил ни один (решение, как стартовать, за вызывающим).
    """
    for url in candidates:
        # Клиент строится ВНУТРИ try. Снаружи он ронял старт целиком: httpx
        # проверяет схему прямо в конструкторе, и опечатка вроде «sock5://»
        # выбрасывала ValueError мимо перебора — бот не поднимался, хотя
        # рабочий запасной канал стоял следующим в том же списке. Кривой
        # логин или пароль так не ловятся (URL разбирается, отказ приходит
        # от прокси), но схему человек путает первым делом.
        try:
            bot = Bot(
                token,
                request=HTTPXRequest(
                    proxy=url,
                    connect_timeout=PROBE_TIMEOUT,
                    read_timeout=PROBE_TIMEOUT,
                ),
            )
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
