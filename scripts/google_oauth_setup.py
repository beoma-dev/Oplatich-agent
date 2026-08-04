#!/usr/bin/env python3
"""Одноразовая OAuth-авторизация Google Drive для ЛИЧНОГО аккаунта.

Зачем: у service account нет квоты хранилища (ограничение Google с 2025) —
файлы счетов должен владеть человеческий аккаунт. Скрипт получает
offline-токен вашего аккаунта; бот использует его ТОЛЬКО для загрузки
файлов в папку счетов. Sheets продолжает работать через service account.

Подготовка (один раз, в Google Cloud Console того же проекта):
  1. APIs & Services → OAuth consent screen: External, добавьте свой
     email в Test users.
  2. Credentials → Create credentials → OAuth client ID → Desktop app →
     скачайте JSON → сохраните как secrets/google_oauth_client.json.

Запуск ЛОКАЛЬНО (нужен браузер): python scripts/google_oauth_setup.py
Скрипт напечатает ссылку — откройте её, разрешите доступ; после редиректа
токен сохранится в secrets/google_oauth_token.json. Скопируйте его на
сервер в secrets/ рядом с service_account.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "secrets" / "google_oauth_client.json"
TOKEN = ROOT / "secrets" / "google_oauth_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    if not CLIENT.exists():
        print(f"❌ Нет {CLIENT}.")
        print("Создайте OAuth client ID (Desktop app) в Google Cloud Console")
        print("и сохраните скачанный JSON по этому пути (см. докстринг).")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=8765,
        open_browser=False,
        authorization_prompt_message="\n👉 Откройте в браузере и разрешите доступ:\n{url}\n",
        access_type="offline",
        prompt="consent",
    )
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    TOKEN.chmod(0o600)
    print(f"✅ Токен сохранён: {TOKEN}")
    print("Скопируйте его на сервер в secrets/ и перезапустите бота.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
