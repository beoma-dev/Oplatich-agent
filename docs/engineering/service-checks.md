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
