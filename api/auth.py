"""Проверка подписи Telegram Mini App initData.

Алгоритм по документации Telegram (Validating data received via the Mini App):
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  hash       = HMAC_SHA256(key=secret_key, msg=data_check_string)
где data_check_string — все поля initData (кроме hash), отсортированные по
ключу и склеенные через \\n как "key=value".

Только так бэкенд убеждается, что запрос пришёл из настоящего Telegram
от конкретного пользователя, а не сфабрикован снаружи.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

from fastapi import HTTPException

# Максимальный возраст initData. Страница формы получает initData при
# открытии; если сотрудник держал форму открытой дольше — попросим переоткрыть.
MAX_AGE_SECONDS = 3600


def validate_init_data(init_data: str, bot_token: str) -> dict:
    """Валидирует initData и возвращает объект пользователя Telegram.

    Бросает HTTPException(401) при любой проблеме с подписью/возрастом.
    """
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Нет данных авторизации Telegram. Откройте форму через кнопку в боте.",
        )

    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Некорректные данные авторизации.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Подпись данных не прошла проверку.")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=401,
            detail="Сессия формы устарела — закройте её и откройте заново.",
        )

    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        user = None
    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(
            status_code=401, detail="Не удалось определить пользователя Telegram."
        )
    return user
