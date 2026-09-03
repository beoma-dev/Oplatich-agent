"""Сборка FastAPI-приложения: API мини-приложения + статика формы.

Приложение живёт в одном процессе с ботом (см. main.py) и получает его
Bot-инстанс через app.state — так API шлёт подтверждения/уведомления от имени
того же бота без второго токена и без webhook.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from telegram import Bot

from api.routes import router

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

# Свои файлы страницы: имя без слэшей и схемы. Ссылку на telegram.org
# (там тоже .js) это выражение не трогает — в ней есть «:» и «/».
_LOCAL_ASSET = re.compile(r'((?:src|href)=")([A-Za-z0-9_.-]+\.(?:js|css|svg))(")')


def asset_version() -> str:
    """Отпечаток статики по СОДЕРЖИМОМУ.

    Пересборка без правок оставляет адреса прежними (кэш продолжает
    работать), а любая правка меняет их все разом.
    """
    digest = hashlib.sha256()
    for path in sorted(WEBAPP_DIR.iterdir()):
        if path.suffix in {".js", ".css", ".svg", ".html"}:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def index_html(version: str) -> str:
    """index.html с версией в адресах своих файлов.

    Без этого WebView Telegram неделями показывает старый JS: заголовок
    `Cache-Control: no-cache` обязывает браузер переспросить сервер, но
    держит страницу живой и переспрашивает не всегда — после деплоя человек
    видел прежний экран и считал, что правку не выкатили. Адрес с версией
    спорить не о чем: файла по такому адресу у него просто нет.
    """
    html = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    return _LOCAL_ASSET.sub(rf"\1\2?v={version}\3", html)


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
    _handle_google_failures(app)

    # Страницу отдаём сами, чтобы подставить версию в адреса файлов.
    # Считаем один раз: в контейнере статика не меняется между запусками.
    page = index_html(asset_version())

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def form_page() -> HTMLResponse:
        return HTMLResponse(page)

    # Остальная статика — в корне; маршруты выше объявлены раньше и имеют
    # приоритет. Запрос вида app.js?v=abc123 отдаётся тем же файлом.
    app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
    return app


def _handle_google_failures(app: FastAPI) -> None:
    """Отказ Google превращаем в честный 503, а не в голый «500».

    Квота Sheets — 60 чтений в минуту на проект, и упереться в неё реально:
    смена статуса читает весь реестр дважды, открытие админ-панели тратит
    несколько чтений. Google отвечает 429, googleapiclient поднимает HttpError,
    и до 03.09.2026 наружу уходило «Internal Server Error» — человек видел
    поломку там, где надо просто подождать минуту.

    Обработчик один на все ручки: перечислять их по одной значит однажды
    забыть новую. Ставится он и на local-бэкенде, где Google не при делах:
    ветка «только для google» означала бы, что в тестах (там всегда local)
    он не выполняется ни разу. Импорт внутри — модуль нужен только здесь.
    """
    from googleapiclient.errors import HttpError

    @app.exception_handler(HttpError)
    async def google_unavailable(request, exc: HttpError) -> JSONResponse:
        status = getattr(getattr(exc, "resp", None), "status", 0)
        # 403 у Google — это и «нет прав», и «слишком часто»; различает reason.
        text = str(exc)
        throttled = status == 429 or (
            status == 403 and ("ateLimit" in text or "uotaExceeded" in text)
        )
        if throttled:
            detail = (
                "Превышен лимит обращений к Google-таблице. "
                "Подождите минуту и повторите."
            )
            log.warning("Google throttling: %s", text[:200])
        else:
            detail = (
                "Реестр сейчас недоступен — Google не ответил. "
                "Попробуйте через минуту."
            )
            log.exception("Google вернул ошибку", exc_info=exc)
        # 503, а не 429: лимит общий на проект, и человек в нём не виноват.
        # Retry-After — не украшение: по нему клиент знает, сколько ждать.
        return JSONResponse(
            {"detail": detail, "retry_after": 60},
            status_code=503,
            headers={"Retry-After": "60"},
        )
