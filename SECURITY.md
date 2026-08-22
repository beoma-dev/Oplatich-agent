# SECURITY — модель угроз invoice-bot

Каждая строка — угроза, мера и место в коде, где мера реализована.
Проверяемые меры покрыты тестами (`tests/`), прогоняются в CI на каждый пуш.

## Идентификация и доступ

| Угроза | Мера | Код |
|---|---|---|
| Подмена пользователя / подача от чужого имени через API | Подпись Telegram initData (HMAC-SHA256 от токена бота), окно 1 час; без валидной подписи — 401 | `api/auth.py` |
| Посторонний подаёт заявку | Whitelist, **fail-closed**: пустой список = не пускать никого (кроме админов из ADMIN_IDS — иначе свежую установку не настроить) | `bot/access.py::is_allowed` |
| Подделка имени: пользователь переименовал Telegram-профиль | Справочник СБ `EMPLOYEE_NAMES` (id → подтверждённое ФИО); в реестр попадает ФИО из справочника, профиль — только fallback | `config.py::employee_name_for`, `bot/handlers.py`, `api/routes.py` |
| Самозахват прав админа (добавить бота в свою группу) | Чат становится доверенным только если бота добавил существующий админ; список корневых админов — только в `.env` | `bot/handlers.py::bot_membership_changed`, `bot/access.py::is_bot_admin` |
| Смена статуса заявки не финансистом | Проверка отправителя callback по списку финансистов + формат ID заявки | `bot/finance_actions.py` |
| Публикация итога в чужой чат (подделка `return_chat`) | Проверка членства автора в чате через `getChatMember` перед отправкой | `services/intake.py::_post_group_summary` |

## Целостность данных

| Угроза | Мера | Код |
|---|---|---|
| Двойная оплата одного счёта (повторная подача) | Дедуп: SHA-256 нормализованных ключевых полей, окно `DEDUP_WINDOW_DAYS`; отправка дубля — только после явного подтверждения | `services/dedup.py`, `bot/handlers.py`, `api/routes.py` |
| Инъекция формул в Google Sheets (`=IMPORTRANGE(...)` в контрагенте) | `valueInputOption="RAW"` — ввод пишется литеральной строкой | `services/google_backend.py::append_invoice_sync` |
| Инъекция формул в xlsx-реестр (открывается в Excel) | Экранирование ячеек, начинающихся с `= + - @` | `bot/models.py::excel_safe`, `services/registry_xlsx.py` |
| Path traversal через имя/«расширение» файла | Имя файла собирается только из безопасных символов | `services/local_storage.py::build_invoice_filename` |
| Порча чужой таблицы СБ | Существующая шапка листа не изменяется; `insertDataOption=OVERWRITE`, чтобы строка не наследовала оформление шапки и data validation | `services/google_backend.py::_ensure_header_sync` |
| Потеря реестра при сбое записи | Атомарное сохранение xlsx (tmp + `os.replace`); доступ сериализован блокировкой фасада | `services/registry_xlsx.py::_atomic_save`, `services/storage.py` |
| Утечка реестра через выгрузку | `/export` и кнопка в панели доступны только админам, файл уходит лично в чат, факт выгрузки — в аудите (REGISTRY_EXPORTED) | `bot/admin.py::export_command`, `api/routes.py::admin_export` |

## Конфиденциальность

| Угроза | Мера | Код |
|---|---|---|
| Данные заявки видны в общем чате | Ввод только в личке/Mini App; в чат уходит краткий итог без файла и реквизитов | `services/intake.py::_post_group_summary` |
| Доступ посторонних к файлам счетов | Drive: файлы наследуют права папки, публичные ссылки не создаются; локально: каталог вне веб-корня, статика отдаёт только `webapp/` | `services/google_backend.py::upload_invoice_file_sync`, `api/server.py` |
| Утечка секретов | `.env`, `secrets/` — в `.gitignore`; тела запросов Telegram не логируются | `.gitignore`, `main.py` |
| Кеширование ответов API | `Cache-Control: no-store` на `/api/*`, nosniff, no-referrer | `api/server.py` |

## Злоупотребления и расследуемость

| Угроза | Мера | Код |
|---|---|---|
| Спам заявками | Rate limit: 5 заявок/мин на пользователя (429) | `api/routes.py::_rate_limited` |
| Нет следов действий («кто пытался и кому отказали») | Аудит-журнал в SQLite: ACCESS/ADMIN/STATUS_DENIED, REQUEST_SUBMITTED/FAILED, STATUS_CHANGED, RATE_LIMITED, DUPLICATE_CONFIRMED — время, user_id, username; просмотр `/audit` | `services/audit.py` |
| Слишком большой файл | Лимит 20 МБ на клиенте, в API и в Caddy (25 МБ) | `bot/validators.py`, `api/routes.py`, `deploy/Caddyfile` |
| Вместо счёта приложили постороннее (котика) | Автопроверка: текст PDF/OCR → скоринг маркеров (заголовок счёта, валидный ИНН, БИК/счета, итог, совпадение суммы с формой); мягкая — предупреждение автору и финансисту + FILE_SUSPICIOUS в аудит, fail-open при сбое | `services/invoice_check.py` |

## Зависимости

Уязвимости в библиотеках — не абстракция: `pypdf` и `Pillow` разбирают
ЗАГРУЖЕННЫЙ пользователем файл (текстовый слой PDF и OCR скана), а
`starlette` с `python-multipart` — входящий HTTP и саму загрузку. Это прямой
путь от чужого файла к парсеру, поэтому версии этих четырёх обновляются в
первую очередь.

Проверять так:

```bash
python -m pip_audit -r requirements.txt
```

Ожидаемый результат — `No known vulnerabilities found`. На версиях от конца
2024 года аудит давал 77 записей.

## Эксплуатация

| Угроза | Мера | Код |
|---|---|---|
| Компрометация процесса = root | Контейнер под непривилегированным пользователем, `no-new-privileges`, `cap_drop: ALL`; systemd-вариант: `ProtectSystem=strict`, `ProtectHome`, запись только в data/storage | `Dockerfile`, `docker-compose.yml`, `deploy/invoice-bot.service` |
| Ошибка конфигурации всплывает на первой заявке | Preflight-проверка токена, хранилища и доступов Google до запуска | `scripts/preflight.py` |

## Правила времени и дат

Все прикладные даты (плановая дата оплаты, метки реестра, аудит) считаются
**на сервере** в `TIMEZONE` (по умолчанию Europe/Moscow), а не в браузере:
клиент передаёт `planned_date=auto`, сервер подставляет «сегодня» (срочно)
или «следующий рабочий день» с переносом пятницы/выходных на понедельник
(`bot/scheduling.py`, покрыто тестами).
