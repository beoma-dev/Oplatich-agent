"""Как называется чат, id которого вписан в настройки сводок.

Панель показывает список адресов, а «-1001481050579» не говорит ничего даже
тому, кто его туда вписал: у всех групп id одинаков на вид и отличается
серединой. Имя спрашиваем у Telegram и держим рядом с id СВОЕЙ копией —
панель обязана открываться и тогда, когда Telegram недоступен, а без копии
список в такую минуту превратился бы обратно в столбик цифр.

Проверка при добавлении здесь же и по той же причине, по которой имя вообще
понадобилось: если бота в чат не позвали (или лишили права писать), сводки
просто не приходят, и понять это можно было только по молчанию. Лучше
отказать сразу и словами.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from services import runtime_settings as rs

log = logging.getLogger(__name__)

# Сколько ждём Telegram, обновляя имена при открытии панели. Имя — украшение
# поверх id, и держать из-за него настройки закрытыми нельзя: не ответил —
# показываем сохранённое.
REFRESH_BUDGET = 3.0

# Статусы, при которых бот в чате есть. "restricted" сюда не входит: он
# означает «в чате, но с урезанными правами», и право писать проверяется
# отдельно — оно и есть то единственное, что нам нужно.
_PRESENT = ("creator", "administrator", "member")


class ChatUnreachable(Exception):
    """Бота в этом чате нет или ему там нечего делать. Текст — для человека."""


async def resolve(bot: Bot, chat_id: int) -> str:
    """Название чата, попутно убеждаясь, что бот сможет туда писать.

    Два вопроса Telegram вместо одного: `get_chat` отвечает и про чат, где
    бот состоит без права слова, — а такой чат в списке сводок бесполезен.
    """
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramError as exc:
        raise ChatUnreachable(
            "Telegram не показывает такой чат. Проверьте id и то, что бот "
            "добавлен в чат."
        ) from exc

    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except TelegramError as exc:
        raise ChatUnreachable("Не удалось проверить, состоит ли бот в чате.") from exc

    allowed = me.status in _PRESENT or (
        me.status == "restricted" and getattr(me, "can_send_messages", False)
    )
    if not allowed:
        raise ChatUnreachable(
            "Бот в этом чате писать не может. Добавьте его в чат и разрешите "
            "отправку сообщений."
        )

    title = (chat.title or chat.full_name or "").strip()
    return title or str(chat_id)


async def _quiet_title(bot: Bot, chat_id: int) -> str | None:
    """Название или None. Сбой Telegram здесь не новость и не ошибка."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramError:
        return None
    return (chat.title or chat.full_name or "").strip() or None


async def entries(bot: Bot, chat_ids: list[int]) -> list[dict]:
    """Список для панели: [{"id": -100…, "title": "BEOMA | Счета"}].

    Имена обновляются на каждом открытии, но в пределах бюджета: чат могли
    переименовать, а показывать старое имя рядом с верным id — обманывать.
    Не успели или Telegram молчит — берём сохранённое.
    """
    if not chat_ids:
        return []
    known = rs.summary_chat_names()
    fresh: list[str | None] = [None] * len(chat_ids)
    try:
        fresh = await asyncio.wait_for(
            asyncio.gather(*(_quiet_title(bot, c) for c in chat_ids)),
            timeout=REFRESH_BUDGET,
        )
    except TimeoutError:
        log.info("Названия чатов сводок не обновились за %s с", REFRESH_BUDGET)

    out: list[dict] = []
    for chat_id, title in zip(chat_ids, fresh, strict=True):
        if title:
            await asyncio.to_thread(rs.set_summary_chat_name, chat_id, title)
        out.append({"id": chat_id, "title": title or known.get(str(chat_id), "")})
    return out
