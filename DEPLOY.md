# Деплой на личный сервер (HTTPS + Mini App)

Форма Mini App требует, чтобы Telegram мог открыть страницу по **HTTPS**.
Рекомендуемый путь — Docker Compose с Caddy: сертификаты Let's Encrypt
выпускаются и продлеваются автоматически, руками ничего делать не нужно.

## Что нужно заранее

1. Linux-сервер с публичным IP (подойдёт любой VPS).
2. Домен (или поддомен), **A-запись** которого указывает на IP сервера,
   например `invoice.example.com`. Без домена Let's Encrypt сертификат не выдаст.
3. Открытые порты **80** и **443** (проверьте firewall/панель хостера).
4. Установленные Docker и Compose-плагин:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

## Шаг 1. Получить проект

```bash
git clone https://github.com/beoma-dev/Oplatich-agent.git invoice-bot
cd invoice-bot
```

## Шаг 2. Заполнить .env

```bash
cp .env.example .env
chmod 600 .env          # в нём токен бота: по умолчанию файл читаем всем
mkdir -p secrets && chmod 700 secrets   # сюда ляжет ключ service account
nano .env
```

Обязательно:

| Переменная | Что поставить |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `DOMAIN` | ваш домен, например `invoice.example.com` |
| `WEBAPP_URL` | `https://<тот же домен>/` |
| `ALLOWED_USER_IDS` | Telegram user_id сотрудников через запятую |
| `FINANCE_CHAT_IDS` | @username или id финансистов |

`STORAGE_DIR` трогать не нужно — в Docker данные автоматически складываются
в `./data` рядом с проектом (файлы счетов, реестр `registry.db` +
xlsx-зеркало, справочник пользователей). **Бэкапить достаточно каталог
`data/`** — встроенный ежедневный бэкап делает именно это.

## Шаг 3. Проверить конфигурацию и запустить

```bash
# Preflight: токен, хранилище, доступы Google — до запуска, а не на первой заявке
docker compose run --rm app python scripts/preflight.py

docker compose up -d --build
```

Контейнер работает от непривилегированного пользователя (uid 1000) — каталог
данных должен ему принадлежать:

```bash
sudo chown -R 1000:1000 data
```

Caddy сам получит сертификат при первом запросе (нужна корректная A-запись).

## Шаг 4. Проверить

```bash
# API отвечает по HTTPS:
curl https://ВАШ_ДОМЕН/api/health        # → {"ok":true}

# Логи бота:
docker compose logs -f app
```

В Telegram: `/start` боту → кнопка «🧾 Подать заявку на оплату» → должна
открыться форма-страница. В группе кнопка ведёт в личку (ограничение Telegram:
web-app кнопки работают только в приватных чатах), итог возвращается в группу.

## Обновление

```bash
git pull
docker compose up -d --build
```

Если реестр вёлся в xlsx до перехода на SQLite (обновление с версий до
2026-08-04) — разово перелейте записи (повторный запуск безопасен):

```bash
docker compose run --rm app python scripts/migrate_xlsx_to_sqlite.py
```

## Бэкапы и восстановление

Ежедневно в `BACKUP_TIME` (по умолчанию 03:30 MSK) бот собирает tar.gz со
всеми данными (`data/backups/`, хранится `BACKUP_KEEP` последних) и присылает
его админам в Telegram. Ручной запуск — команда `/backup`.

Проверка, что архив пригоден к восстановлению (разворачивает копию во
временный каталог и удаляет её за собой, боевые данные не трогает):

```bash
docker compose run --rm app python scripts/verify_backup.py
```

Скрипт читает реестр и базу из архива, сверяет их между собой и проверяет,
что у каждой заявки на месте её PDF. Прогонять после изменений в составе
данных и раз в квартал просто так: бэкап, который ни разу не разворачивали,
— это гипотеза, а не бэкап.

Восстановление на новом сервере:

```bash
git clone … invoice-bot && cd invoice-bot   # + заполнить .env
mkdir -p data && tar -xzf invoice-bot-backup-*.tar.gz -C data
sudo chown -R 1000:1000 data
docker compose up -d --build
```

## Подключение Google (таблица + папка со счетами)

При `STORAGE_BACKEND=google` реестр живёт в Google-таблице, а файлы счетов —
в папке Диска. Нужны **ключи доступа**, и их количество зависит от того, где
лежит папка.

### Почему может понадобиться два ключа, а не один

У service account с 2025 года **нет собственной квоты хранилища**. Файл,
который он загружает в папку личного Google-аккаунта, принадлежал бы ему
самому — и Google отказывает: «Service Accounts do not have storage quota».

Отсюда две схемы, и код поддерживает обе (`services/google_backend.py`,
`_drive_credentials`): если в `secrets/` лежит OAuth-токен человека, Drive
работает от его имени; если нет — от service account.

### Путь А — общий диск (Google Workspace). Один ключ

Самый простой и без истекающих токенов. Годится, если у вас корпоративный
Google (домен вида `@вашафирма.ru`).

1. Google Drive → **Общие диски** → создать диск, например «Оплатыч».
   Перенести туда папку со счетами. Файлы общего диска не расходуют квоту
   service account — проблема исчезает.
2. Google Cloud Console → проект (новый или существующий) →
   **APIs & Services → Library**: включить **Google Sheets API** и
   **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create**: имя любое, роли не нужны.
4. Открыть созданный аккаунт → **Keys → Add key → Create new key → JSON**.
   Скачанный файл положить на сервер как `secrets/service_account.json`.
5. Скопировать email аккаунта (вида `…@….iam.gserviceaccount.com`) и выдать
   ему **Редактор**: на общий диск (или папку) и на таблицу реестра.

### Путь Б — папка в «Моём диске» (личный аккаунт). Два файла

Шаги 2–5 из пути А выполняются так же — service account нужен для таблицы.
Дополнительно готовим OAuth-токен человека для загрузки файлов:

6. Cloud Console → **APIs & Services → OAuth consent screen**. Если аккаунт
   корпоративный — тип **Internal** (тогда токен не истекает). Если личный —
   **External** и свой email в **Test users**; учтите ловушку ниже.
7. **Credentials → Create credentials → OAuth client ID → Desktop app** →
   скачать JSON → сохранить как `secrets/google_oauth_client.json`.
8. **На своём компьютере** (нужен браузер), в копии проекта:
   ```bash
   python scripts/google_oauth_setup.py
   ```
   Открыть напечатанную ссылку, разрешить доступ. Появится
   `secrets/google_oauth_token.json` — скопировать его на сервер в `secrets/`.

**Ловушка режима Testing:** у External-приложения в статусе «Testing»
refresh-токен живёт **7 дней**, после чего загрузка файлов перестаёт
работать. Лечение: либо тип Internal (нужен Workspace), либо опубликовать
приложение, либо раз в неделю повторять шаг 8 — последнее плохо и годится
только на время проверки.

### Прописать в .env

```bash
STORAGE_BACKEND=google
GOOGLE_CREDENTIALS_FILE=secrets/service_account.json
GOOGLE_SHEET_ID=<id из URL таблицы>
GOOGLE_DRIVE_FOLDER_ID=<id из URL папки>
# только для пути Б; по умолчанию этот путь и так подставляется
GOOGLE_OAUTH_TOKEN_FILE=secrets/google_oauth_token.json
```

ID берутся прямо из адресной строки:
`docs.google.com/spreadsheets/d/`**`ЭТО_ID`**`/edit`,
`drive.google.com/drive/folders/`**`ЭТО_ID`**.

Права на файлы ключей:

```bash
chmod 700 secrets && chmod 600 secrets/*.json
```

### Проверить ДО переключения

```bash
docker compose run --rm app python scripts/preflight.py
```

Проверка только читает: получает название таблицы и имя папки. Если доступа
нет — скажет об этом, ничего не сломав. Гонять её можно и на боевом,
оставаясь на `STORAGE_BACKEND=local`, — она проверяет доступ, а не пишет.

### Порядок переключения

1. `preflight` зелёный.
2. Меняем `STORAGE_BACKEND=google`, `docker compose up -d`.
3. Подаём одну заявку и глазами проверяем: строка в таблице, файл в папке,
   ссылка в карточке открывается.
4. Старые локальные данные никуда не исчезают — `data/` остаётся на месте,
   и вернуться на `local` можно тем же переключателем.

Шапку существующей таблицы бот **не переписывает**: заголовки он добавляет
только в пустой лист (`_ensure_header_sync`). Свои служебные колонки он
пишет правее девяти колонок из ТЗ.

## Стенд (второй бот на этой же машине)

Нужен ровно для того, чего не умеет CI: проверить сторону Telegram (BotFather,
Mini App в живом клиенте, кнопки карточек) и отрепетировать выкатку до того,
как её увидит боевой контур.

Устроен так, что боевые команды о нём не знают: стенд описан в отдельном
`docker-compose.stage.yml`, а `docker compose up -d --build` его не читает.
Caddy подключает сайты стенда по маске `deploy/conf.d/*.caddy` — пока файла
нет, стенда для него не существует (маска без совпадений валидна, проверено
`caddy validate`).

### Что понадобится

1. **Отдельный бот** в [@BotFather](https://t.me/BotFather): `/newbot`. Тот же
   токен, что у боевого, не подойдёт — два опроса одного бота конфликтуют.
2. **A-запись** поддомена (например `stage.invoice.example.com`) на этот же
   сервер. Сертификат Caddy выпустит сам.
3. **Тестовый чат** для карточек финансиста — чтобы проверочные заявки не
   попадали настоящим финансистам.

### Настройка

```bash
cp .env.stage.example .env.stage && chmod 600 .env.stage
# заполнить TELEGRAM_BOT_TOKEN, WEBAPP_URL, FINANCE_CHAT_IDS, ADMIN_IDS

cp deploy/stage.caddy.example deploy/conf.d/stage.caddy
echo 'STAGE_DOMAIN=stage.invoice.example.com' >> .env   # рядом с DOMAIN

mkdir -p data-stage && sudo chown -R 1000:1000 data-stage
```

### Запуск и проверка

```bash
docker compose -f docker-compose.yml -f docker-compose.stage.yml up -d stage
docker compose up -d caddy          # подхватить сайт стенда

# смоук: подать заявку и убедиться, что она прошла весь путь
docker compose -f docker-compose.yml -f docker-compose.stage.yml \
  exec stage python scripts/smoke_stage.py

docker compose -f docker-compose.yml -f docker-compose.stage.yml logs -f stage
docker compose -f docker-compose.yml -f docker-compose.stage.yml stop stage
```

`ENV_LABEL=СТЕНД` в `.env.stage` рисует над формой красную плашку: два бота
выглядят одинаково, и спутать их — значит подать настоящую заявку в пустоту
или наоборот. Смоук-скрипт отказывается работать при пустом `ENV_LABEL` —
это его защита от запуска на боевом.

### Ресурсы

Замеры на этой машине: экземпляр бота — 73 МБ при старте, 93 МБ на разборе
текстового счёта, **162 МБ на пике распознавания скана A4 300 dpi**. Поэтому
стенду поставлены `mem_limit: 512m` и `cpus: "1.0"`: ядер на машине два, и
одно всегда остаётся боевому контуру, чем бы стенд ни занимался. Образ общий
— отдельного места на диске он не занимает, только свой каталог `data-stage/`.

### Порядок работы

```
правка → CI (тесты) → стенд (живой Telegram + смоук) → бой
```

Боевые данные на стенд не переносим: это финансовые документы и персональные
данные сотрудников. Нужен объём для проверок роста — генерируйте синтетику.

## Очистка тестовых данных перед прод-запуском

После тестового периода в системе остаются документы, которых в проде быть
не должно: PDF заявок, загруженные счета, строки реестра, отпечатки дедупа,
карточки в чатах финансистов. Инструмент — `scripts/purge_data.py`. По
умолчанию он НИЧЕГО не удаляет: печатает подробный отчёт, что будет очищено
(количества, размеры, периоды, разбивки, примеры записей).

```bash
# 1. Посмотреть, что вообще есть (документы и их следы)
docker compose exec app python scripts/purge_data.py

# 2. Посмотреть по всем целям, включая аудит, бэкапы, справочник
docker compose exec app python scripts/purge_data.py --preset all --full

# 3. Удалить. Бота на это время останавливаем: он пишет в те же файлы
#    и держит часть состояния в памяти
docker compose stop app
docker compose run --rm --no-deps app python scripts/purge_data.py --apply --yes
docker compose start app
```

Перед удалением собирается страховочный архив в `data/backups/` (тот же
формат, что `/backup`) — восстановление описано выше. Отключить: `--no-backup`.

Цели (`--only`/`--skip`, через запятую):

| Ключ | Что стирает | В `--preset documents` |
|---|---|---|
| `files` | PDF заявок и загруженные счета в `STORAGE_DIR` | да |
| `registry` | xlsx-реестр (создастся заново с шапкой) | да |
| `requests` | строки заявок в SQLite — источник правды | да |
| `reasons` | причины отклонения/отложения | да |
| `dedup` | отпечатки дедупа (иначе повтор тестовой заявки = дубль) | да |
| `cards` | ссылки на карточки в чатах финансистов | да |
| `audit` | журнал безопасности: доступы, отказы, удаления | нет |
| `conversations` | незаконченные черновики форм | нет |
| `incidents` | журнал сбоев в карточке «Здоровье бота» | нет |
| `backups` | архивы `data/backups` (в них те же тестовые данные) | нет |
| `users` | `known_users.json`: @username → chat_id | нет |

Чего очистка НЕ делает: не трогает `.env`, состав финансистов, whitelist,
админов, напоминания и настройки бэкапа; не удаляет сами сообщения-карточки
из чатов финансистов (после очистки их кнопки отвечают «Заявка не найдена» —
удалите старые карточки в чате руками). При `STORAGE_BACKEND=google` заявки
и файлы лежат в Sheets и Drive: скрипт чистит только локальные данные и для
`--apply` требует флаг `--allow-google`, таблицу и папку нужно очищать вручную.

## Внешний мониторинг (UptimeRobot)

Алерты бота приходят через Telegram — если умер сам контейнер или прокси
до Telegram, они не дойдут. Поэтому доступность должен проверять кто-то
**снаружи** сервера. `GET https://<домен>/api/health` для этого различает:

| Ответ | Что случилось |
|---|---|
| 200 | всё живо |
| 503 (`ok: false`) | процесс жив, но Telegram недоступен — обычно умер WARP/прокси |
| 502 / таймаут | умер контейнер, сервер или Caddy |

Настройка [UptimeRobot](https://uptimerobot.com) (бесплатный тариф, ~5 минут):

1. Зарегистрируйтесь → **Add New Monitor**.
2. Monitor Type: **HTTP(s)**; URL: `https://<ваш домен>/api/health`;
   Interval: **5 minutes**.
3. В Alert Contacts добавьте email (или Telegram-бота UptimeRobot) — алерт
   придёт по независимому от вашего сервера каналу.

Любой не-200 ответ (в т.ч. 503) считается «down» — падение и контейнера,
и прокси даёт уведомление в течение ≤ 5 минут.

## Запасной прокси (фейловер)

`PROXY_URL` принимает **несколько адресов через запятую** — при старте бот
проверяет их по порядку коротким `getMe` и подключается через первый
отвечающий:

```bash
PROXY_URL=socks5://warp:1080,socks5://172.17.0.1:1080
```

Типичная связка: основной — WARP (вариант A ниже), запасной — свой SOCKS5
через `ssh -D` на любом зарубежном VPS (вариант B). Если основной умер,
достаточно перезапустить контейнер (`docker compose restart app`) — бот сам
уйдёт на запасной; вместе с мониторингом выше простой сводится к минутам.

## Полезное

- **Выключить Mini App** (вернуть пошаговую чат-форму): очистите `WEBAPP_URL`
  в `.env` и перезапустите. HTTPS при этом не нужен вовсе.
- **Локальная разработка**: `WEBAPP_URL` пустой, `python main.py` — бот работает
  без API и без HTTPS.
- **Протестировать Mini App без сервера**: пробросьте локальный порт наружу
  туннелем (`cloudflared tunnel --url http://localhost:8080` или ngrok) и
  укажите выданный HTTPS-адрес в `WEBAPP_URL`.

## Открытие формы прямо из канала/группы (Direct Link Mini App)

По умолчанию кнопка в группе/канале ведёт в личку с ботом (web_app-кнопки там
запрещены Telegram). Чтобы форма открывалась **сразу поверх чата**:

1. В [@BotFather](https://t.me/BotFather): `/newapp` → выберите бота → задайте
   название, описание, картинку 640×360 (готовая —
   `assets/brand/cover-640x360.png`; размер BotFather проверяет строго) →
   **Web App URL** = ваш `WEBAPP_URL` → короткое имя (например, `invoice`).
2. В `.env`: `MINIAPP_SHORT_NAME=invoice` и перезапустите
   (`docker compose up -d`).
3. Переопубликуйте кнопку: пост `/menu` в канале/группе.

Кнопка станет прямой ссылкой `t.me/<бот>/invoice` — форма открывается в один
тап (при самом первом запуске Telegram спросит подтверждение). Итог заявки
по-прежнему возвращается в чат, из которого открыли форму.

## Если api.telegram.org недоступен с сервера

Симптом: контейнер падает на старте с таймаутом (`getMe`), API на 8080 не
поднимается, HTTPS отдаёт 502. Типично для части российских хостеров.

Бот поддерживает прокси: `PROXY_URL` в `.env` (SOCKS5 или HTTP) — через него
идут и вызовы Bot API, и long polling.

### Вариант A — бесплатно, без второго сервера: Cloudflare WARP

1. Сервис `warp` и volume `warp_data` уже описаны в `docker-compose.yml` и
   включены (если у вас свой compose/сеть — добавьте контейнер
   `caomingjun/warp` в ту же сеть, что и бот).
2. В `.env`: `PROXY_URL=socks5://warp:1080`
3. `docker compose up -d` и проверка:
   ```bash
   docker compose exec warp curl -x socks5h://127.0.0.1:1080 -sS https://api.telegram.org
   docker compose logs -f app
   ```
   Первый запуск WARP регистрируется ~30 секунд. Если WARP у хостера тоже
   зарезан — остаётся вариант B.

### Вариант B — свой SOCKS5 через любой зарубежный VPS (ssh -D)

На сервере бота (`172.17.0.1` — адрес docker-бриджа; для другой сети возьмите
gateway из `docker network inspect`):

```bash
ssh -N -D 172.17.0.1:1080 user@foreign-vps   # в проде — через autossh/systemd
```

В `.env`: `PROXY_URL=socks5://172.17.0.1:1080`. Не привязывайте `-D` к
`0.0.0.0` без файрвола — SOCKS станет публичным.

Примечания: MTProto-прокси (t.me/proxy) для Bot API **не подходят** — нужен
именно SOCKS5/HTTP. Публичные бесплатные прокси из списков для продакшена не
годятся: нестабильны и часто уже забанены Telegram.

## Вариант без Docker (systemd + Caddy/nginx)

1. Python 3.11+, каталог `/opt/invoice-bot`:
   ```bash
   sudo useradd -r -m -d /opt/invoice-bot invoicebot
   sudo -u invoicebot git clone https://github.com/beoma-dev/Oplatich-agent.git /opt/invoice-bot
   cd /opt/invoice-bot
   sudo -u invoicebot python3 -m venv .venv
   sudo -u invoicebot .venv/bin/pip install -r requirements.txt
   sudo -u invoicebot cp .env.example .env   # и заполнить
   ```
2. Юнит: `sudo cp deploy/invoice-bot.service /etc/systemd/system/ && sudo systemctl enable --now invoice-bot`
3. Реверс-прокси с HTTPS — любой:
   - **Caddy** (сам получает сертификаты): `invoice.example.com { reverse_proxy 127.0.0.1:8080 }` в `/etc/caddy/Caddyfile`;
   - **nginx + certbot**:
     ```nginx
     server {
         server_name invoice.example.com;
         client_max_body_size 25m;
         location / {
             proxy_pass http://127.0.0.1:8080;
             proxy_set_header Host $host;
             proxy_set_header X-Forwarded-Proto $scheme;
         }
     }
     ```
     затем `certbot --nginx -d invoice.example.com`.
