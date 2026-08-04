#!/usr/bin/env python3
"""Healthcheck для Docker: жив ли встроенный HTTP API.

При выключенном Mini App (WEBAPP_URL пуст) API нет — считаем контейнер
здоровым, если процесс жив (бот и uvicorn живут в одном процессе: смерть
любого из них завершает процесс, и restart-политика поднимает контейнер).
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

if not settings.webapp_enabled:
    sys.exit(0)

try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{settings.api_port}/api/health", timeout=5
    ) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
