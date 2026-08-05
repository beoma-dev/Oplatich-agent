# AGENTS.md — инструкции для AI-агентов

## Что это

Telegram-бот подачи заявок на оплату счетов. Два канала ввода — форма Mini App
(страница `webapp/` + API `api/`) и пошаговая чат-форма (`bot/handlers.py`);
оба сходятся в `services/intake.py::finalize_submission`. Бот называется
**Оплатыч**, его марка — `webapp/logo.svg` (отдаётся статикой из `webapp/`,
используется в шапке формы, favicon и пустых списках). Краткое описание
архитектуры по пунктам ТЗ и стоимость реализации — в
[ARCHITECTURE.md](ARCHITECTURE.md), полное описание решений и их причин — в
[CONTEXT.md](CONTEXT.md), деплой — в [DEPLOY.md](DEPLOY.md).

## Команды

```bash
# Запуск (нужен .env с TELEGRAM_BOT_TOKEN; пустой WEBAPP_URL = без API/HTTPS)
python main.py

# Линт
ruff check .

# Тесты (токен подставит tests/conftest.py, внешний не нужен)
pytest -q
node --test "tests/js/*.test.cjs"

# Смоук-проверка импортов (без реального токена)
TELEGRAM_BOT_TOKEN=dummy:token python -c "import main, api.server, bot.handlers, services.intake"
```

## Конвенции

- Python 3.11+: типы PEP 604 (`X | None`), `zoneinfo`, async/await.
- Комментарии, докстринги и все тексты для пользователя — **на русском**.
- Синхронный I/O (файлы) — только через `asyncio.to_thread`, не блокировать
  event loop.
- Любая пользовательская строка перед отправкой с `parse_mode=HTML` — через
  `html.escape`.
- В логи не попадают суммы, реквизиты, содержимое файлов и токены.

## Инварианты — менять синхронно, не ломать

- **Валидация зеркалится в трёх местах**: `bot/validators.py` (сервер),
  `webapp/index.html` (JS-зеркало: CURRENCIES, ARTICLES, лимиты длин, форматы
  файлов, парсинг суммы/даты), `api/routes.py` (использует серверные
  валидаторы). Меняешь поле формы — правишь все три + `bot/models.py`.
- **Реестр и файлы — только через `services/storage.py`** (фасад: local =
  SQLite + xlsx-зеркало, google = Sheets + xlsx-зеркало); модули
  `google_backend`/`registry_sqlite`/`registry_xlsx` напрямую из хендлеров
  не дёргать. Первые девять колонок `SHEET_HEADERS` — ровно по ТЗ, порядок
  не менять. CSV упразднён.
- Порядок колонок реестра: `SHEET_HEADERS` ↔ `InvoiceRequest.as_sheet_row()`
  (bot/models.py) — единый источник, менять парой.
- `POST /api/invoice` защищён подписью Telegram initData (`api/auth.py`).
  Новые маршруты, принимающие данные пользователя, обязаны проверять подпись
  так же. Маршруты «своих» данных (`/api/my-requests`, `/api/my/withdraw`)
  берут автора ТОЛЬКО из проверенного initData — никогда из тела запроса.
  `/api/finance/*` показывает чужие заявки — доступ строго через
  `is_financier()` или `is_bot_admin()`, отказ пишется в аудит.
- **Состав финансистов, whitelist и админов — только через
  `services/runtime_settings.py`**: `effective_*_ids()` = запись из `.env`
  плюс добавленные из панели, минус отозванные (`*_off`). Читать
  `settings.admin_ids`/`allowed_user_ids` напрямую там, где решается «есть ли
  право», нельзя — назначенный из панели админ окажется бесправным.
  Исключение по замыслу: у админов нет списка отключённых — запись из
  `ADMIN_IDS` панелью не снимается, владелец сервера остаётся владельцем.
- **Запрос доступа — один сервис `services/access_requests.py`**: и кнопка
  в чате, и `/api/access/request` зовут `request_access`, решение админа —
  `resolve_access`. Ручки `/api/access*` проверяют подпись initData, но НЕ
  whitelist: они существуют ровно для того, у кого доступа ещё нет.
- **Смена статуса — только через `services/status_change.apply_status`**:
  им пользуются и кнопки карточки в чате, и панель Mini App; иначе каналы
  разъедутся в поведении (карточки, причина, аудит, уведомление автору).
- **Статусы заявки зеркалятся**: `bot/models.py` (`REQUEST_STATUSES`,
  `STATUS_NEW`, `STATUS_WITHDRAWN`), `bot/my_requests.py` (`_STATUS_ICONS`),
  `webapp/index.html` (`STATUS_ICON`, `STATUS_CLASS`, `STATUS_ACTIONS`) +
  раскраска в
  `registry_xlsx.py` и `google_backend._style_requests`. Анти-дрейф стережёт
  `tests/test_webapp_sync.py::test_statuses_mirror`.
- **Бета-функции гасятся одним тумблером** и не меняют основной сценарий:
  автозаполнение (`services/invoice_extract`) только ПРЕДЛАГАЕТ значения,
  подстановку делает пользователь, и заполняются лишь пустые поля.
- **Тесты не ходят в Google**: `tests/conftest.py` принудительно ставит
  `STORAGE_BACKEND=local` и гасит `GOOGLE_*`. Не убирать: репозиторий бывает
  развёрнут на сервере рядом с боевым `.env`, и без этого `pytest -q` пишет
  тестовые заявки в настоящий реестр.
- Переменные окружения документируются в `.env.example` (единственный
  справочник) и читаются только через `config.py`.
- Относительные пути (`STORAGE_DIR` и т.п.) разрешаются от каталога проекта
  через свойства `config.Settings`, не от CWD.

## Не трогать

- `.env`, `secrets/` — секреты; `data/`, `storage/` — рабочие данные
  (реестр заявок, файлы счетов, справочник пользователей).
- Не коммитить и не логировать содержимое этих каталогов.

## Перед коммитом

1. `ruff check .`
2. Смоук-импорт (команда выше).
3. Если менялись поля заявки — проверить все четыре точки: чат-форма,
   `webapp/index.html`, `api/routes.py`, `bot/models.py`.
