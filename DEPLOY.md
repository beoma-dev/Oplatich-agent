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

Восстановление на новом сервере:

```bash
git clone … invoice-bot && cd invoice-bot   # + заполнить .env
mkdir -p data && tar -xzf invoice-bot-backup-*.tar.gz -C data
sudo chown -R 1000:1000 data
docker compose up -d --build
```

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

1. Раскомментируйте сервис `warp` и volume `warp_data` в `docker-compose.yml`
   (если у вас свой compose/сеть — добавьте контейнер `caomingjun/warp` в ту же
   сеть, что и бот).
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
