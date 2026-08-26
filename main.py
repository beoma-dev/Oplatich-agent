"""Точка входа: Telegram-бот подачи счетов (+ HTTP API мини-приложения).

Режимы:
  - WEBAPP_URL задан  → в одном процессе работают бот (long polling) и
    FastAPI-сервер, который отдаёт страницу формы Mini App и принимает заявки.
    HTTPS терминируется снаружи (Caddy/nginx) — см. DEPLOY.md.
  - WEBAPP_URL пуст   → только бот с пошаговой чат-формой (локальная разработка).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    TypeHandler,
    filters,
)

from bot.access_requests import ask_access_callback, resolve_access_callback
from bot.admin import (
    admin_command,
    allow_command,
    audit_command,
    backup_command,
    deny_command,
    export_command,
    fin_add_command,
    fin_del_command,
    myid_command,
)
from bot.commands import help_command
from bot.finance_actions import (
    CB_REASON_SKIP,
    reason_message,
    reason_skip_callback,
    status_callback,
)
from bot.handlers import (
    CB_HELP,
    bot_membership_changed,
    build_conversation_handler,
    channel_command,
    help_popup,
    show_menu,
)
from bot.my_requests import CB_WITHDRAW, my_command, withdraw_callback
from config import settings
from services import alerts, backup, health, reminders
from services.access_requests import CB_ASK
from services.proxy import build_requests, masked, pick_working_proxy
from services.user_directory import remember

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
# Не логируем тела запросов Telegram/Google (могут содержать конфиденциальные данные).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

log = logging.getLogger("invoice-bot")


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободное сообщение вне формы в личке — подсказка. Заявкой НЕ считается."""
    # В группе не отвечаем на каждый текст, чтобы не спамить.
    chat = update.effective_chat
    if chat and chat.type != "private":
        return
    await update.message.reply_text(
        "Чтобы подать заявку на оплату, отправьте /invoice или нажмите /menu."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Необработанная ошибка", exc_info=context.error)
    # Алерт админам (с троттлингом) — тихих падений быть не должно.
    if context.error is not None:
        where = ""
        if isinstance(update, Update) and update.effective_user is not None:
            where = f"апдейт от id {update.effective_user.id}"
        await alerts.alert_error(context.bot, context.error, where)


async def start_background_tasks(application: Application) -> None:
    """Фоновые задачи: автобэкап, пульс Telegram, напоминания о сроках.

    post_init/post_shutdown PTB срабатывают только в run_polling-режиме,
    поэтому webapp-режим вызывает start/stop вручную (см. _run_bot_with_api).
    """
    application.bot_data["_backup_task"] = asyncio.create_task(
        backup.backup_loop(application.bot)
    )
    application.bot_data["_health_task"] = asyncio.create_task(
        health.probe_loop(application.bot)
    )
    application.bot_data["_reminder_task"] = asyncio.create_task(
        reminders.reminder_loop(application.bot)
    )


async def stop_background_tasks(application: Application) -> None:
    for key in ("_backup_task", "_health_task", "_reminder_task"):
        task = application.bot_data.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def resolve_proxy() -> str:
    """Рабочий прокси из PROXY_URL (с фейловером) или "" — прямое подключение.

    Единственный кандидат используется без проверки (как раньше); при
    нескольких пробуем по порядку. Если не ответил ни один — стартуем через
    первый: сеть могла мигнуть, PTB будет переподключаться сам.
    """
    candidates = settings.proxy_urls
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]
    chosen = await pick_working_proxy(settings.telegram_bot_token, candidates)
    if chosen is None:
        log.critical(
            "Ни один прокси из PROXY_URL не ответил — стартую через первый (%s); "
            "Telegram может быть недоступен",
            masked(candidates[0]),
        )
        return candidates[0]
    return chosen


async def collect_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запоминает @username → id из каждого апдейта (для резолва финансистов).

    Работает в отдельной группе (group=-1) и не мешает остальным хендлерам.
    Запись файла — в пуле потоков, чтобы не блокировать event loop.
    """
    user = update.effective_user
    # Ботов не запоминаем: свои же посты в канале приходят обратно апдейтом,
    # и бот попадал в справочник наравне с людьми.
    if user is not None and not user.is_bot:
        await asyncio.to_thread(remember, user.id, user.username)


def build_application(proxy_url: str | None = None) -> Application:
    # proxy_url — уже выбранный resolve_proxy() адрес; None = взять первый
    # кандидат из настроек (путь для тестов и прямых вызовов).
    # Персистентность диалогов: рестарт не обрывает начатые чат-формы.
    # Храним только user_data (bot_data содержит несериализуемые задачи).
    persistence = PicklePersistence(
        filepath=settings.security_db_path.parent / "conversations.pickle",
        store_data=PersistenceInput(
            bot_data=False, chat_data=False, user_data=True, callback_data=False
        ),
        update_interval=30,
    )
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .persistence(persistence)
        .post_init(start_background_tasks)
        .post_shutdown(stop_background_tasks)
    )
    if proxy_url is None:
        proxy_url = settings.proxy_urls[0] if settings.proxy_urls else ""
    # Свои клиенты вместо .proxy(): в них зашит повтор вызовов, которые до
    # Telegram не дошли (services/proxy.RetryingRequest). Канал теряет
    # единицы процентов вызовов, и без повтора это видно человеку как
    # «нажал кнопку — ничего не произошло». Ставим всегда, с прокси и без:
    # повтор от наличия прокси не зависит.
    api_request, polling_request = build_requests(proxy_url)
    builder = builder.request(api_request).get_updates_request(polling_request)
    if proxy_url:
        log.info("Telegram API — через прокси: %s", masked(proxy_url))
    app = builder.build()

    # group=-1: собираем справочник пользователей раньше основных хендлеров,
    # не потребляя апдейт (обработка в group=0 продолжится).
    app.add_handler(TypeHandler(Update, collect_user), group=-1)

    app.add_handler(CommandHandler("menu", show_menu))
    # /help — список команд под права конкретного человека.
    app.add_handler(CommandHandler("help", help_command))
    # «Мои заявки»: список своих заявок со статусами, отзыв и повтор.
    app.add_handler(CommandHandler("my", my_command))
    # Админ-команды (личка) и /myid для всех.
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("fin_add", fin_add_command))
    app.add_handler(CommandHandler("fin_del", fin_del_command))
    app.add_handler(CommandHandler("allow", allow_command))
    app.add_handler(CommandHandler("deny", deny_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("audit", audit_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("backup", backup_command))
    # Команды в постах КАНАЛА: CommandHandler ловит только message, не
    # channel_post — поэтому отдельный обработчик по регэкспу.
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.Regex(r"^/(menu|invoice|start)(@\w+)?\b"),
            channel_command,
        )
    )
    # Кнопка «Инструкция» — всплывающее окно, работает вне диалога.
    app.add_handler(CallbackQueryHandler(help_popup, pattern=rf"^{CB_HELP}$"))
    # Кнопки статуса на карточке финансиста (✅/⏸/❌) + причина отклонения.
    app.add_handler(CallbackQueryHandler(status_callback, pattern=r"^ST:"))
    app.add_handler(CallbackQueryHandler(reason_skip_callback, pattern=rf"^{CB_REASON_SKIP}$"))
    # Кнопка «🚫 Отозвать» в списке «Мои заявки» (повтор — вход в диалог формы).
    app.add_handler(CallbackQueryHandler(withdraw_callback, pattern=rf"^{CB_WITHDRAW}:"))
    app.add_handler(CallbackQueryHandler(ask_access_callback, pattern=rf"^{CB_ASK}$"))
    app.add_handler(
        CallbackQueryHandler(resolve_access_callback, pattern=r"^ACQ:(ok|no):")
    )
    # group=-2: причина от финансиста перехватывается ДО формы и fallback'а;
    # если причин не ждём — хендлер молчит, и апдейт идёт дальше.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, reason_message
        ),
        group=-2,
    )
    # Приветствие с кнопкой, когда бота добавили в группу ИЛИ канал.
    # my_chat_member покрывает оба случая (NEW_CHAT_MEMBERS в каналах не бывает).
    app.add_handler(
        ChatMemberHandler(bot_membership_changed, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    # /start и /invoice — точки входа внутри ConversationHandler.
    app.add_handler(build_conversation_handler())
    # Fallback ставим последним, чтобы он не перехватывал шаги диалога.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))
    app.add_error_handler(on_error)
    return app


async def _run_bot_with_api() -> None:
    """Бот (polling) + API мини-приложения в одном event loop.

    uvicorn ставит обработчики SIGINT/SIGTERM и по сигналу возвращается из
    serve() — после этого аккуратно гасим бота.
    """
    import uvicorn

    from api.server import build_api

    application = build_application(await resolve_proxy())
    api_app = build_api(application.bot)
    server = uvicorn.Server(
        uvicorn.Config(
            api_app,
            host=settings.api_host,
            port=settings.api_port,
            # Не даём uvicorn перенастроить logging — используем basicConfig выше.
            log_config=None,
        )
    )

    async with application:  # initialize() / shutdown()
        # post_init здесь не срабатывает (только в run_polling) — фоновые
        # задачи (автобэкап, пульс Telegram) запускаем вручную.
        await start_background_tasks(application)
        # Кнопка меню чата (слева от поля ввода) открывает форму Mini App.
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Заявка", web_app=WebAppInfo(url=settings.webapp_url)
            )
        )
        try:
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info(
                "Бот запущен (polling), API мини-приложения на http://%s:%s (форма: %s)",
                settings.api_host, settings.api_port, settings.webapp_url,
            )
            await server.serve()
        finally:
            await stop_background_tasks(application)
            if application.updater and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()


def main() -> None:
    log.info("Запуск бота подачи счетов…")
    if settings.webapp_enabled:
        asyncio.run(_run_bot_with_api())
    else:
        log.info("WEBAPP_URL не задан — работает только чат-форма.")
        proxy = asyncio.run(resolve_proxy())
        build_application(proxy).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
