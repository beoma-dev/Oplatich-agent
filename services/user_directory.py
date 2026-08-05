"""Справочник @username → chat_id.

Telegram Bot API не даёт отправлять личные сообщения по @username — только по
числовому chat_id, и только тем, кто сам взаимодействовал с ботом. Поэтому бот
запоминает соответствие @username → id для всех, кого «видит» (кто пишет боту
в личку или появляется в группе), и по этому справочнику резолвит получателей
срочных уведомлений, заданных через @username.

Хранится в JSON-файле, переживает перезапуск.
"""
from __future__ import annotations

import json
import logging
import threading

from config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, int] | None = None


def _load_locked() -> dict[str, int]:
    global _cache
    if _cache is None:
        path = settings.user_directory_path
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                _cache = {str(k).lower(): int(v) for k, v in raw.items()}
            except (ValueError, OSError):
                log.exception("Не удалось прочитать справочник %s — начинаю с пустого", path)
                _cache = {}
        else:
            _cache = {}
    return _cache


def remember(user_id: int, username: str | None) -> None:
    """Запоминает @username → id. Пишет файл только при изменении."""
    if not username:
        return
    key = username.lower().lstrip("@")
    with _lock:
        cache = _load_locked()
        if cache.get(key) == user_id:
            return
        cache[key] = user_id
        path = settings.user_directory_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("Справочник: запомнил @%s → %s", key, user_id)
        except OSError:
            log.exception("Не удалось сохранить справочник %s", path)


def username_for(user_id: int) -> str | None:
    """Обратный резолв: @username по id, если пользователь встречался боту."""
    with _lock:
        for name, uid in _load_locked().items():
            if uid == user_id:
                return f"@{name}"
    return None


def all_users() -> list[tuple[int, str]]:
    """Все, кого бот встречал: (id, @username), по алфавиту.

    Справочник заполняется при любом обращении к боту, поэтому это список
    тех, кто вообще с ним общался, — основа для админского списка доступа.
    """
    with _lock:
        pairs = [(uid, f"@{name}") for name, uid in _load_locked().items()]
    return sorted(pairs, key=lambda pair: pair[1].lower())


def resolve(entry: str) -> int | None:
    """Резолвит запись получателя в chat_id.

    Принимает числовой id ("12345", "-100...") или @username.
    Возвращает None, если username ещё не встречался боту.
    """
    e = entry.strip().lstrip("@")
    if not e:
        return None
    if e.isdigit() or (e.startswith("-") and e[1:].isdigit()):
        return int(e)
    with _lock:
        return _load_locked().get(e.lower())
