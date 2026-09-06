"""Проверка адреса api.telegram.org, прибитого в /etc/hosts контейнера warp.

Зачем пин. Прокси (gost внутри warp) берёт у резолвера РОВНО ОДИН адрес и
не перебирает остальные — проверено: сорок запросов подряд ушли в заведомо
мёртвый первый адрес, ко второму он не притронулся. DNS отдаёт AAAA первой,
путь до IPv6 Telegram через WARP не работает, и каждый десятый вызов
терялся на таймауте. Прибитая A-запись убирает AAAA из выдачи.

Заменить пин настройкой резолвера нельзя, и это проверено (reports/005,
R17): выключение IPv6 в netns роняет сам туннель, `/etc/gai.conf` gost не
читает (собран статически, cgo-резолвера в нём нет), флаг `-I` на выбор
семейства адресов не влияет.

Почему проверяем ДОСТИЖИМОСТЬ, а не совпадение с DNS. 25.08.2026 выяснилось
опытом: через одну сессию WARP адрес из DNS (149.154.166.110) не отвечал
вовсе, а другой адрес Telegram (149.154.167.220) отвечал. Пин выбирается по
тому, докуда WARP реально доходит, и с выдачей DNS совпадать не обязан —
сравнение с DNS давало бы ложную тревогу каждый день. Значение имеет одно:
отвечает ли прибитый адрес. Если перестанет — бот замолчит целиком, и
выбрать новый адрес нужно тем же способом, каким выбран этот.
"""
from __future__ import annotations

import logging
import socket

import httpx

from config import settings
from services import proxy
from services.proxy import masked

log = logging.getLogger(__name__)

TELEGRAM_HOST = "api.telegram.org"
PROBE_TIMEOUT = 15.0


def resolve_v4(host: str = TELEGRAM_HOST) -> list[str]:
    """Живые A-записи хоста — для контекста в сообщении, не для вердикта."""
    try:
        infos = socket.getaddrinfo(host, 443, family=socket.AF_INET)
    except OSError as exc:
        log.warning("DNS не ответил по %s: %s", host, exc)
        return []
    return sorted({info[4][0] for info in infos})


def reachable() -> bool:
    """Отвечает ли Telegram по тому пути, которым ходит бот.

    Идём ровно через прокси и ровно по имени: значит, через прибитый адрес.
    Любой ответ HTTP годится — нам важен факт соединения, а не код.
    """
    try:
        # Ровно тот канал, который выбрал бот. НЕ settings.proxy_url: там сырая
        # строка, и при нескольких кандидатах через запятую httpx принимает её
        # за один адрес, не соединяется — и проверка врала «пин не отвечает».
        with httpx.Client(proxy=proxy.active() or None, timeout=PROBE_TIMEOUT) as c:
            c.get(f"https://{TELEGRAM_HOST}/")
        return True
    except Exception:  # noqa: BLE001 — любой отказ означает «не дошли»
        return False


def check() -> tuple[bool, str]:
    """(всё ли в порядке, строка для человека)."""
    pinned = (settings.telegram_pinned_ip or "").strip()
    if not pinned:
        return True, "Пин адреса Telegram не используется."

    if reachable():
        live = resolve_v4()
        note = ""
        if live and pinned not in live:
            # Не ошибка: пин и выбирается вне DNS. Но знать полезно.
            note = f" (DNS сейчас отдаёт {', '.join(live)})"
        return True, f"Пин {pinned} отвечает{note}."

    channel = masked(proxy.active()) or "прямое подключение"
    return False, (
        f"Telegram не отвечает по имени через текущий канал ({channel}). "
        f"Если канал — WARP, виноват прибитый адрес {pinned}: gost ходит "
        f"только по нему, и бот замолчит целиком. Подберите адрес, до "
        f"которого WARP доходит, и поправьте extra_hosts у warp в "
        f"docker-compose.yml вместе с TELEGRAM_PINNED_IP в .env. Если канал "
        f"платный прокси — пин ни при чём, проверяйте сам прокси. "
        f"Живой DNS: {', '.join(resolve_v4()) or 'не ответил'}."
    )
