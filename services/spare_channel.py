"""Проверка ЗАПАСНОГО канала до Telegram: жив ли он, пока не понадобился.

Активный канал проверяет пульс (services/health) раз в минуту, и о его
провале узнают сразу. А запасной не проверяет НИКТО: `resolve_proxy`
перебирает кандидатов только при старте и останавливается на первом
рабочем, `preflight` делает ровно то же. Пока основной жив, про запасной
не известно ничего — а пользуются им в единственный момент, когда основной
уже отказал. Выяснять тогда, что и запасного нет, поздно вдвойне: канал,
которым сообщают о сбое, — это и есть отказавший канал.

30.09.2026 эту слепоту увидели вживую на стенде. Его WARP умер 16.09
(cgroup убил демона за перерасход памяти), а gost перед мёртвым демоном
продолжал принимать соединения и отваливаться по таймауту за ними. Снаружи
всё выглядело живым; две недели никто не знал, что канала нет, и правку
интерфейса пришлось откатывать вслепую — проверить её было негде.

Вторая причина, боевая: через WARP доходит РОВНО ОДИН адрес Telegram —
тот, что прибит в extra_hosts (см. docker-compose.yml). Выведут его из
обращения — запасной канал умрёт молча, DNS тут ничего не подскажет, и
заметить это может только такая проверка.

Об активном канале отсюда НЕ сообщаем: его провал — работа пульса, и два
источника одного сообщения расходятся в формулировках и в пороге.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot

from config import settings
from services import alerts, proxy, recovery
from services import runtime_settings as rs

log = logging.getLogger(__name__)

# Раз в сутки: запасной канал не отказывает внезапно в ответ на нагрузку,
# он тихо гниёт неделями. Чаще — лишние походы наружу без новой информации.
CHECK_INTERVAL = 24 * 3600.0
# Не на самом старте: при подъёме бот и так выбирает канал, и лишняя проба
# в ту же секунду только удлиняет старт.
FIRST_DELAY = 300.0

TITLE = "Резервный канал до Telegram недоступен"
HINT = "Проверьте резерв: до успешной проверки этот адрес нельзя считать рабочим."


async def check_spares(bot: Bot) -> dict:
    """Проверяет все каналы, кроме активного. Алерт — если нерабочие есть.

    Возвращает срез для панели и тестов: сколько проверено, кто жив, кто нет.
    """
    candidates = settings.proxy_urls
    if len(candidates) < 2:
        # Один канал — запасного нет, и проверять нечего. Молчим: это
        # настройка, а не поломка.
        return {"checked": 0, "alive": [], "dead": [], "reason": "запасного канала нет"}

    active = proxy.active()
    alive: list[str] = []
    dead: list[tuple[str, str]] = []
    for url in candidates:
        if url == active:
            continue
        reason = await proxy.probe_proxy(settings.telegram_bot_token, url)
        for delay in (2, 4):
            if not reason:
                break
            await asyncio.sleep(delay)
            reason = await proxy.probe_proxy(settings.telegram_bot_token, url)
        key = "proxy:" + proxy.masked(url)
        # Перенос последнего явного алерта старой версии: адрес берём только
        # из действующей настройки. Заменённый прокси не объявляем исправленным.
        if await asyncio.to_thread(recovery.get_sync, key) is None:
            for incident in await asyncio.to_thread(rs.recent_incidents, 60):
                if (incident.get("title") in (TITLE, "Запасной канал до Telegram не отвечает")
                        and proxy.masked(url) in incident.get("details", "")
                        and not incident.get("recovered_ts")):
                    await asyncio.to_thread(
                        recovery.fail_sync, key, "telegram", incident["title"],
                        incident.get("first_ts", incident["ts"]),
                    )
                    break
        if reason:
            await recovery.fail(key, "telegram", TITLE)
            dead.append((proxy.masked(url), reason))
        else:
            alive.append(proxy.masked(url))
            await recovery.recover(
                bot, key, "Канал до Telegram восстановлен",
                f"Прокси {proxy.masked(url)} снова отвечает на getMe.",
            )

    result = {
        "checked": len(alive) + len(dead),
        "alive": alive,
        "dead": [m for m, _ in dead],
        "reason": "",
    }
    if not dead:
        log.info("Запасные каналы проверены, отвечают: %s", ", ".join(alive) or "—")
        return result

    lines = "\n".join(f"• {mask} — {why}" for mask, why in dead)
    where = proxy.masked(active) if active else "неизвестен"
    await alerts.alert_admins(
        bot,
        TITLE,
        f"Не отвечает резерв:\n{lines}\n\nОсновной канал: {where}. Его доступность проверяет отдельный пульс.",
        kind="telegram",
        icon="⚠️",
        # Своя подпись: троттлинг по умолчанию считает повтором похожий текст,
        # а нам важно, чтобы напоминание приходило по КАЖДОМУ мёртвому каналу
        # и повторялось, пока его не починят.
        signature="spare:" + ",".join(m for m, _ in dead),
        hint=HINT,
    )
    log.warning("Запасной канал не отвечает: %s", "; ".join(f"{m} ({w})" for m, w in dead))
    return result


async def spare_loop(bot: Bot) -> None:
    """Фоновая задача: раз в сутки проверяет запасные каналы."""
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            await check_spares(bot)
        except Exception:  # noqa: BLE001 — проверка датчика не роняет бота
            log.exception("Проверка запасного канала сорвалась")
        await asyncio.sleep(CHECK_INTERVAL)
