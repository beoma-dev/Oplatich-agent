"""Проверка подписи Telegram initData: валидная, подделанная, просроченная."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi import HTTPException

from api.auth import validate_init_data

TOKEN = "123456:TESTTOKEN"


def _signed_init_data(token: str = TOKEN, age_seconds: int = 0, user: dict | None = None) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age_seconds),
        "query_id": "AAH-test",
        "user": json.dumps(
            user if user is not None else {"id": 42, "first_name": "Тест", "username": "tester"},
            ensure_ascii=False,
        ),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def test_valid_signature():
    user = validate_init_data(_signed_init_data(), TOKEN)
    assert user["id"] == 42
    assert user["username"] == "tester"


def test_forged_hash_rejected():
    data = _signed_init_data()
    forged = data[:-4] + ("0000" if not data.endswith("0000") else "1111")
    with pytest.raises(HTTPException) as exc:
        validate_init_data(forged, TOKEN)
    assert exc.value.status_code == 401


def test_wrong_bot_token_rejected():
    with pytest.raises(HTTPException):
        validate_init_data(_signed_init_data(token="999:OTHER"), TOKEN)


def test_expired_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_init_data(_signed_init_data(age_seconds=7200), TOKEN)
    assert exc.value.status_code == 401


def test_empty_rejected():
    with pytest.raises(HTTPException):
        validate_init_data("", TOKEN)


def test_user_required():
    with pytest.raises(HTTPException):
        validate_init_data(_signed_init_data(user={"no_id": True}), TOKEN)
