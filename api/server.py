"""Сборка FastAPI-приложения: API мини-приложения + статика формы.

Приложение живёт в одном процессе с ботом (см. main.py) и получает его
Bot-инстанс через app.state — так API шлёт подтверждения/уведомления от имени
того же бота без второго токена и без webhook.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from telegram import Bot

from api.routes import router

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


def build_api(bot: Bot) -> FastAPI:
    app = FastAPI(
        title="invoice-bot api",
        # Публичный сервис: схемы и своагер наружу не выставляем.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bot = bot

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Ответы API содержат данные заявок — их не кешируем вовсе.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            # Статике достаточно ОБЯЗАТЕЛЬНОЙ ревалидации. Раньше здесь тоже
            # стоял no-store, потому что WebView Telegram держит страницу
            # цепко и после деплоя показывал бы старый JS неделями. Но
            # no-cache даёт ту же гарантию свежести — браузер обязан
            # спросить сервер, — и при этом неизменные файлы возвращаются
            # как 304 вместо 82 КБ тела на каждое открытие формы
            # (замеры reports/005, R20). ETag сервер отдаёт и так.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(router, prefix="/api")
    # Статика формы — в корне; маршруты /api/* объявлены раньше и имеют приоритет.
    app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
    return app
