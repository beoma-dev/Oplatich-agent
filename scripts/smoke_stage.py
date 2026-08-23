#!/usr/bin/env python3
"""Смоук-проверка стенда: подать заявку и убедиться, что она везде легла.

«Проверил на стенде» должно быть командой, а не настроением. Скрипт делает
то же, что живой сотрудник: подписывает initData токеном СТЕНДОВОГО бота,
отправляет заявку в его API и затем проверяет, что она есть в реестре, в
xlsx-зеркале и что реестр с зеркалом сходятся.

    docker compose -f docker-compose.yml -f docker-compose.stage.yml \
      run --rm stage python scripts/smoke_stage.py

Запускать ТОЛЬКО на стенде: проверка пишет настоящую заявку в тот реестр,
который видит. Отказывается работать, если ENV_LABEL пуст, — это признак
боевого контура.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

FAILURES = 0


def say(good: bool, text: str) -> None:
    global FAILURES
    if not good:
        FAILURES += 1
    print(("  ✓ " if good else "  ✗ ") + text)


def signed_init_data(user_id: int) -> str:
    """initData, подписанный токеном этого бота, — как из настоящего клиента."""
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH-smoke",
        "user": json.dumps(
            {"id": user_id, "first_name": "Смоук", "username": "smoke"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def post_invoice(base: str, init_data: str, marker: str) -> dict:
    boundary = "----smoke"
    fields = {
        "amount": "1234,56",
        "currency": "RUB",
        "counterparty": f"ООО «Смоук {marker}»",
        "article": "Прочее",
        "planned_date": "auto",
        "work_deadline": "проверка стенда",
        "comment": f"Автопроверка стенда {marker}",
        "urgency": "NORMAL",
        "has_invoice": "0",
        "requisites": "Счёт 40702810000000000001, БИК 044525225",
    }
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        f"{base}/api/invoice",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Telegram-Init-Data": init_data,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        # Без тела ответа проверка бесполезна: «422» не говорит, что не так.
        detail = err.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {err.code}: {detail}") from err


def main() -> int:
    if not settings.env_label.strip():
        print("ENV_LABEL пуст — это похоже на боевой контур. Проверка отменена.")
        return 1
    print(f"Смоук-проверка контура «{settings.env_label.strip()}»\n")

    base = f"http://127.0.0.1:{settings.api_port}"
    user_id = settings.admin_ids[0] if settings.admin_ids else 1
    marker = time.strftime("%H%M%S")

    try:
        answer = post_invoice(base, signed_init_data(user_id), marker)
    except Exception as exc:  # noqa: BLE001
        say(False, f"заявка не отправилась: {exc}")
        return 1
    request_id = answer.get("request_id", "")
    say(bool(request_id), f"заявка принята: {request_id}")

    from services import registry_check, storage

    ids = asyncio.run(storage.all_request_ids())
    say(request_id in ids, f"есть в реестре (всего заявок {len(ids)})")

    row = asyncio.run(storage.get_request(request_id))
    say(row is not None, "читается по своему ID")
    if row is not None:
        say(
            f"Смоук {marker}" in row.get("Контрагент", ""),
            f"контрагент на месте: {row.get('Контрагент', '—')}",
        )
        say(row.get("Статус оплаты") == "Новая", f"статус: {row.get('Статус оплаты')}")

    result = asyncio.run(registry_check.check())
    say(result.get("ok", False), registry_check.describe(result))

    print()
    if FAILURES:
        print(f"ИТОГ: провалов — {FAILURES}. Стенд не готов принимать выкатку.")
        return 1
    print("ИТОГ: стенд отвечает, заявка проходит весь путь.")
    print(f"Уберите проверочную заявку {request_id} из панели, если она мешает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
