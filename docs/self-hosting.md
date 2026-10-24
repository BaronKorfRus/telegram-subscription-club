# Self-hosting

Инструкция для собственного сервера: Docker, `.env`, контент, Caddy, бэкап,
мониторинг и обновления.

## Требования

- Linux-сервер с Docker и Docker Compose.
- Домен, направленный на сервер.
- Telegram-бот, канал и чат (бот должен быть админом).
- Терминал Т-Банка: тестовый для проверки и боевой для приёма платежей.
- PostgreSQL поднимается в compose; отдельный сервер не обязателен.

## Переменные окружения

Скопируйте `.env.example` в `.env` и заполните значения. Файл `.env` не
попадает в git.

| Переменная | Назначение |
| --- | --- |
| `DATABASE_URL` | Подключение к PostgreSQL. В compose хост `db`. |
| `BOT_TOKEN` | Токен Telegram-бота. |
| `TELEGRAM_PROXY` | SOCKS5-прокси для Telegram, если нужен. |
| `OWNER_TG_ID` | Telegram ID владельца: получает отчёты и заявки админов. |
| `CHANNEL_ID` | ID канала (отрицательное число). |
| `CHAT_ID` | ID чата (отрицательное число). |
| `CHANNEL_INVITE_LINK` | Ссылка-приглашение в канал. |
| `DOMAIN` | Домен для Caddy; подставляется в `{$DOMAIN}`. |
| `TBANK_TERMINAL_KEY` | Ключ терминала Т-Банка. |
| `TBANK_PASSWORD` | Пароль терминала для подписи. |
| `TBANK_NOTIFICATION_URL` | Публичный URL `/tbank/notify`. |
| `PUBLIC_BASE_URL` | Публичный базовый URL сервиса. |
| `HEARTBEAT_URL` | URL внешнего сторожа. |
| `HEARTBEAT_TOKEN` | Токен хартбита. |
| `TZ` | Часовой пояс клуба, по умолчанию `Europe/Moscow`. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Параметры контейнера PostgreSQL. |
| `SALEBOT_API_KEY`, `SALEBOT_API_BASE` | Только для миграции из Salebot. |
| `BACKUP_DIR`, `BACKUP_REMOTE` | Каталог бэкапов и необязательный удалённый получатель. |
| `AMNEZIA_SSH_*`, `TGPROXY_KEY_PATH` | Необязательный SSH-прокси для Telegram. |
| `SUMMARY_BULK_THRESHOLD` | Порог, после которого сводка считается крупной. |
| `CONTENT_DIR` | Каталог с `texts.yaml` и `tariffs.yaml`. |

Не коммитьте `.env`, ключи, токены и ID в репозиторий.

## Контент

В репозитории есть только пример `content/example`. Для своего клуба:

```bash
cp -r content/example content
# отредактируйте content/texts.yaml и content/tariffs.yaml
```

Укажите в `.env`:

```text
CONTENT_DIR=./content
```

Формат `tariffs.yaml`:

```yaml
- code: m1
  title: <название тарифа>
  price_kopecks: <цена в копейках>
  period_days: 30
  is_legacy: false
  is_active: true
  sort_order: 1
```

Допустимые сроки — 30, 90 и 180 дней. Цена должна быть положительным целым
числом в копейках. Коды уникальны.

После миграций и после добавления новых кодов загрузите тарифы в базу:

```bash
docker compose run --rm -T web python - <<'PY'
from app.db import SessionLocal
from app.seed import seed_tariffs

with SessionLocal() as session:
    seed_tariffs(session)
    session.commit()
PY
```

`seed_tariffs` добавляет только отсутствующие коды. Существующие строки не
перезаписываются: чтобы изменить цену или название уже загруженного тарифа,
поправьте строку в базе или добавьте новый код. Перезапуск сервисов сам по
себе тарифы не загружает.

В `texts.yaml` должны быть все ключи, которые требует загрузчик. Если ключа
нет, приложение сообщит об ошибке и не стартует. Пример содержит полный
набор.

### Монтирование контента в Docker

Образ не содержит `content/`, поэтому смонтируйте каталог в контейнеры.
Создайте `docker-compose.override.yml`:

```yaml
services:
  web:
    volumes:
      - ./content:/app/content:ro
  bot:
    volumes:
      - ./content:/app/content:ro
  billing:
    volumes:
      - ./content:/app/content:ro
```

Если вы собираете собственный образ, можно добавить `COPY content ./content`
в `Dockerfile` и пересобирать образ при изменении контента.

## Сети и Caddy

`docker-compose.yml` ожидает внешнюю сеть `edge`:

```bash
docker network create edge 2>/dev/null || true
```

`ops/club.caddy` — шаблон сайта:

```text
{$DOMAIN} {
    reverse_proxy club-web:8000
}
```

Установите его в конфигурацию Caddy (например, в `edge/sites/`) и
перезагрузите Caddy. Переменная `DOMAIN` должна быть в окружении процесса
Caddy, а не только в `.env` приложения. Caddy сам получит TLS-сертификат.

`web` доступен в сети `edge` как `club-web:8000`; наружу порт не публикуется.

## Первый запуск

```bash
cp .env.example .env
cp -r content/example content
# заполните .env
docker network create edge 2>/dev/null || true
docker compose up -d db
docker compose run --rm web alembic upgrade head
# загрузите тарифы из content/tariffs.yaml в базу
docker compose run --rm -T web python - <<'PY'
from app.db import SessionLocal
from app.seed import seed_tariffs

with SessionLocal() as session:
    seed_tariffs(session)
    session.commit()
PY
# без прокси tgproxy не запускаем
docker compose up -d web bot billing heartbeat
```

Если нужен `tgproxy`, подготовьте ключи (см. ниже) и запустите
`docker compose up -d` — поднимется весь набор сервисов.

Проверки:

```bash
docker compose ps
docker compose exec web python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
curl -fsS "https://<ваш-домен>/healthz"
```

Ожидается `200` и JSON вида
`{"status":"ok","db":true,"billing_age_sec":...,"bot_age_sec":...}`.

## Telegram

1. Создайте бота и получите `BOT_TOKEN`.
2. Добавьте бота админом в канал и чат.
3. Узнайте ID канала и чата и заполните `CHANNEL_ID`, `CHAT_ID`.
4. Подготовьте ссылку-приглашение и заполните `CHANNEL_INVITE_LINK`.
5. Бот работает через long polling, поэтому вебхук нужно снять:

```bash
curl -sS "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"
curl -sS "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

В `getWebhookInfo.result.url` должно быть пусто. Если вебхук оставить,
бот и старый получатель будут конфликтовать за обновления.

### Прокси для Telegram

Если сервер не имеет прямого доступа к Telegram, в compose есть сервис
`tgproxy` — SSH SOCKS5-туннель.

1. Положите приватный ключ в `secrets/tgproxy`, а `known_hosts` — в
   `secrets/tgproxy_known_hosts` (права `600`).
2. Заполните `AMNEZIA_SSH_HOST`, `AMNEZIA_SSH_USER`, при необходимости
   `AMNEZIA_SSH_PORT` и `TGPROXY_KEY_PATH`.
3. Укажите `TELEGRAM_PROXY=socks5://tgproxy:1080`.
4. Запустите `docker compose up -d`.

Схема `socks5h` не поддерживается клиентом aiogram — используйте `socks5`.
Если прокси не нужен, оставьте `TELEGRAM_PROXY` пустым и не запускайте
`tgproxy`.

## Т-Банк

1. Создайте тестовый и боевой терминалы.
2. Заполните `TBANK_TERMINAL_KEY`, `TBANK_PASSWORD`,
   `TBANK_NOTIFICATION_URL`.
3. В кабинете терминала включите HTTP-уведомления и укажите
   `https://<ваш-домен>/tbank/notify`.
4. Проверьте подпись и приём на тестовом терминале.
5. Перед включением боевого биллинга проведите тест 1 ₽ с возвратом.

Подробнее — [tbank.md](tbank.md).

## Бэкап

`ops/backup.sh` делает `pg_dump`, сжимает его и удаляет файлы старше 14 дней.
Если задан `BACKUP_REMOTE`, копия уезжает на удалённый хост.

```bash
BACKUP_DIR=./backups ./ops/backup.sh
```

Поставьте скрипт в cron, например раз в сутки ночью. Проверяйте, что бэкап
создаётся и что из него можно восстановиться. Если бэкапы не должны лежать в
репозитории, укажите в `BACKUP_DIR` отдельный каталог или отправляйте копию
на удалённый хост через `BACKUP_REMOTE`.

Восстановление:

```bash
gunzip -c ./backups/ИМЯ_ФАЙЛА.sql.gz \
  | docker compose exec -T db psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```

Перед восстановлением остановите `bot` и `billing`, чтобы не было новых
записей.

## Мониторинг

- `/healthz` — доступность приложения и базы.
- `heartbeat` раз в минуту проверяет `/healthz` и отправляет POST на
  `HEARTBEAT_URL` с заголовками `X-Heartbeat-Token` и
  `Authorization: Bearer <token>`.
- Внешний сторож должен жить вне этого сервера. Если сигнала нет 3 минуты —
  тревога.
- Логи: `docker compose logs -f web bot billing heartbeat`.
- Ежедневная сводка приходит владельцу и админам.
- Ошибки доставки сообщений видны в таблице `notification_errors`.

## Обновления

1. Сделайте бэкап базы.
2. Выполните `./deploy.sh` или вручную:

```bash
git pull --ff-only
docker compose build
docker compose up -d db
docker compose run --rm web alembic upgrade head
docker compose up -d
docker compose ps
```

Если `tgproxy` не используется, последнюю команду замените на
`docker compose up -d web bot billing heartbeat` и так же поправьте
`deploy.sh`.

3. Проверьте `/healthz`, логи и тестовый платёж.
4. Если что-то сломалось, остановите `bot` и `billing`, верните предыдущий
   образ/коммит и восстановите базу из бэкапа.

Миграции применяются только вперёд. Перед обновлением с изменениями схемы
всегда делайте бэкап.

## Чек-лист безопасности

- `.env` с правами `600`, не в git.
- Отдельные ключи для тестового и боевого терминалов.
- HTTPS только через Caddy, без самоподписанных сертификатов.
- База не опубликована наружу.
- SSH по ключам, пароль и root-вход отключены.
- Регулярные обновления зависимостей и образа.
- Бэкапы хранятся вне сервера.
- Доступ к каналу и чату — только через бота.
- При подозрении на утечку ключей: остановить `billing`, сменить ключи,
  проверить `payment_events` и `payments`.
