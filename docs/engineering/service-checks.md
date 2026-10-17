# Проверки Oplatich-agent
Python 3.11; установка — requirements.txt. Инструменты CI: ruff, pytest,
pytest-asyncio, pip-audit. JS: npm ci, Node 22.

- ruff check .
- pytest -q
- npm test
- TELEGRAM_BOT_TOKEN=dummy:token python -c "import main, api.server, bot.handlers, services.intake"
- При изменениях Mini App: npx playwright install chromium; npm run test:e2e.
- Аудит зависимостей: pip-audit -r requirements.txt.

Тесты принудительно используют local backend в tests/conftest.py; эту защиту сохранять.
Не брать боевой .env. Health: GET /api/health на настроенном WEBAPP_URL,
значение ok означает недавний ответ Telegram, а не проверку всех пользовательских сценариев.
Деплой и сохранность рабочих файлов — DEPLOY.md, RUNBOOK.md и корневые инструкции.

## Единая команда local / CI

Полная проверка из корня репозитория — `./check`. Требования: Python 3.11, Node.js 22, npm и локальный Docker engine.
Первичная подготовка чистого клона:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt ruff pytest pytest-asyncio pip-audit
./check
```

Ruff, pytest, smoke imports, pip-audit, npm unit, Chromium E2E и Docker build.
`./check python`, `./check web`, `./check image` — отдельные части того же набора;
CI запускает все три параллельно. Сборка создаёт локальный образ invoice-bot:check; remote Docker host/context
отклоняется до build (приоритет контекста учитывается).
На Linux один раз установи системные библиотеки браузера: `npm ci && npx playwright install-deps chromium`.
Аудит обращается к базе уязвимостей, npm/Chromium/Docker могут загружать зависимости.

Ненулевой exit code означает, что проверка не прошла. Отсутствие Docker, браузера,
зависимостей или сети не считается успешным пропуском. Выполняй в отдельном
клоне без боевого .env, токенов, браузерных профилей и рабочих данных.

Python берётся из активного окружения, иначе из .venv/, иначе python3.
Для явного выбора интерпретатора: `PYTHON=/absolute/path/to/python ./check`.
