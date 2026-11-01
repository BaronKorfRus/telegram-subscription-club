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
| `HEALTH_STALE_SECONDS` | Сколько секунд без отметки живости считается нормой; по умолчанию `180`. |
| `HEALTHZ_TOKEN` | Ключ подробного `/healthz`; пусто — подробный отчёт закрыт всем. |
| `TZ` | Часовой пояс клуба, по умолчанию `Europe/Moscow`. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Параметры контейнера PostgreSQL. |
| `SALEBOT_API_KEY`, `SALEBOT_API_BASE` | Только для миграции из Salebot. |
| `AMNEZIA_SSH_*`, `TGPROXY_KEY_PATH` | Необязательный SSH-прокси для Telegram. |
| `SUMMARY_BULK_THRESHOLD` | Порог, после которого сводка считается крупной. |
| `PAYMENT_WINDOW_START_DAY` | Первый день окна оплаты и якорный день окончания периода; по умолчанию `22`. |
| `PAYMENT_WINDOW_END_DAY` | Последний день окна оплаты; по умолчанию `24`. |
| `CONTENT_DIR_HOST` | Путь на хосте к каталогу с `texts.yaml` и `tariffs.yaml`; compose монтирует его в `/content`. |
| `CONTENT_DIR` | Каталог с контентом внутри контейнера; compose задаёт `/content`. Без Docker — `./content`. |

Не коммитьте `.env`, ключи, токены и ID в репозиторий.

## Контент

В репозитории есть только пример `content/example`. Для своего клуба:

```bash
cp -r content/example/. content
# отредактируйте content/texts.yaml и content/tariffs.yaml
```

Укажите в `.env` путь к этому каталогу на хосте:

```text
CONTENT_DIR_HOST=./content
```

`docker-compose.yml` монтирует `${CONTENT_DIR_HOST:-./content}:/content:ro` и
передаёт сервисам `CONTENT_DIR=/content`. Если `CONTENT_DIR_HOST` не задан,
используется `./content`. При запуске без Docker укажите в `.env`
`CONTENT_DIR=./content`.

Формат `tariffs.yaml`:

```yaml
- code: m1
  title: <название тарифа>
  price_kopecks: <цена в копейках>
  period_days: 30
  period_months: 1
  is_legacy: false
  is_active: true
  sort_order: 1
```

Допустимые сроки — 30, 90 и 180 дней (`period_days`) и 1, 3 и 6 месяцев
(`period_months`). `period_days` остаётся для совместимости и исторических
тарифов, а длительность периода считается по `period_months`. Цена должна
быть положительным целым числом в копейках. Коды уникальны.

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

## Окно оплаты и периоды

Оплата и вход в канал открыты только в дни окна — по умолчанию **22, 23 и
24 число каждого месяца** целиком, по часовому поясу из `TZ`. Дни настраиваются:

```text
PAYMENT_WINDOW_START_DAY=22
PAYMENT_WINDOW_END_DAY=24
```

- Вне окна регистрация работает: бот сохраняет клиента и показывает, когда
  откроется оплата, вместо тарифов.
- Вызов оплаты вне окна отклоняется (исключение `WindowClosedError`), так
  что обойти окно через API нельзя. Исключение — замена карты: она доступна
  в любой день.
- Подписка всегда заканчивается в `PAYMENT_WINDOW_START_DAY` месяца: оплата
  24 октября даёт период до 22 ноября, 22 ноября — до 22 декабря. Тарифы на
  3 и 6 месяцев отсчитываются от этой же даты.
- Кто зарегистрировался, но не оплатил в окне, получает напоминания в
  `START_DAY - 3` (по умолчанию 19-го) и в день открытия окна (22-го), в
  12:00 по `TZ`. Напоминания идемпотентны по дате: повторный запуск воркера
  не дублирует рассылку.

Истечение подписки вынесено за пределы окна: воркер перепроверяет оплату
**25-го** (задача `expire_check`), поэтому доступ открыт все дни окна.
Автосписание, выпавшее вне окна, не списывается, а переносится на ближайшее
`PAYMENT_WINDOW_START_DAY` в 09:00.

Если вы уже разворачивали проект и хотите привести существующие даты к
якорному дню, используйте разовый скрипт (сначала посмотрите таблицу, потом
примените; перед `--apply` сделайте бэкап базы):

```bash
docker compose run --rm -T web python -m scripts.align_periods
docker compose run --rm -T web python -m scripts.align_periods --apply
```

Скрипт берёт только подписки со статусом `active` и `past_due`, переносит
дату окончания на ближайший якорный день, удаляет устаревшие `pending`
задачи цикла и ставит новый. Повторный запуск ничего не дублирует.

### Монтирование контента в Docker

Образ не содержит `content/`, поэтому `docker-compose.yml` монтирует каталог
из `CONTENT_DIR_HOST` в `/content` только для чтения. Отдельный
`docker-compose.override.yml` не нужен.

Если меняете `CONTENT_DIR_HOST` или содержимое каталога, перезапустите
сервисы, чтобы они перечитали контент:

```bash
docker compose up -d web bot billing
```

Для локального запуска достаточно скопировать пример:

```bash
cp -r content/example/. content
CONTENT_DIR_HOST=./content
```

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
cp -r content/example/. content
# в .env.example уже указан CONTENT_DIR_HOST=./content
# заполните остальные значения
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

Выгрузку и восстановление делает отдельный проект backup-services: он
вызывает `ops/backup-export.sh` и `ops/backup-import.sh`. Вручную их тоже
можно запустить.

Выгрузка в папку:

```bash
./ops/backup-export.sh /путь/к/копии
```

В папке появляется `db.dump` — дамп базы в формате `pg_dump -Fc`. Контент
(тексты, тарифы, когорты, списки) хранится в git-репозитории и в архив не
входит: восстанавливать его из копии базы не нужно.

Если кроме базы нужно сохранять отдельный приватный каталог, задайте его
переменной окружения `CLUB_PRIVATE_DIR` — тогда рядом появится `private.tgz`.
Без переменной архив не создаётся; если каталог задан, но его нет на диске,
архив пропускается без ошибки:

```bash
CLUB_PRIVATE_DIR=/путь/к/приватному/каталогу ./ops/backup-export.sh /путь/к/копии
```

Для копии только базы добавьте аргумент `db`:

```bash
./ops/backup-export.sh /путь/к/копии db
```

Восстановление из папки останавливает `web`, `bot`, `billing` и `heartbeat`,
заливает базу через `pg_restore --clean --if-exists --no-owner` и снова
поднимает сервисы:

```bash
./ops/backup-import.sh /путь/к/копии
```

Если задан `CLUB_PRIVATE_DIR`, дополнительно распаковывается `private.tgz`:
каталог предварительно очищается, поэтому его нужно указать тот же, что при
выгрузке. Без переменной — или если переменная задана, но каталога нет на
диске — восстанавливается только база. С аргументом `db`
приватный каталог не трогается в любом случае. Восстановление затирает
текущие данные: перед ним нужна свежая копия. Проверяйте, что из копии
действительно можно восстановиться.

## Мониторинг

- `/healthz` — доступность базы и живость фоновых сервисов. Бот и биллинг раз
  в 30 секунд пишут отметку в `service_heartbeats`; если отметка старше
  `HEALTH_STALE_SECONDS` (по умолчанию 180), ответ будет `503`.
- Без ключа ответ короткий: `{"status":"ok"}` или `{"status":"error"}` —
  только код и общий статус. Подробный отчёт (`db`, `services`,
  `billing_age_sec`, `bot_age_sec`, `problems` и `components` с очередью задач
  и платежами) отдаётся с заголовком `Authorization: Bearer <HEALTHZ_TOKEN>`.
  Если `HEALTHZ_TOKEN` пуст, подробный отчёт не отдаётся никому.
- Код ответа от подробностей не зависит: `503` — когда база или фоновые
  сервисы не в порядке либо превышены необязательные пороги
  `HEALTH_MAX_BILLING_AGE_SEC` / `HEALTH_MAX_BOT_AGE_SEC`. Просроченная задача
  или зависший платёж видны в `components` и `problems`, но контейнер не
  переводят в нездоровые.
- Пример подробного ответа:
  `{"status":"warn","problems":["jobs"],"components":{"db":{"status":"ok"},"bot":{"status":"ok","age_sec":4,"limit_sec":180},"billing":{"status":"ok","age_sec":7,"limit_sec":180},"jobs":{"status":"warn","overdue":0,"failed_24h":1,"next_charge_in_sec":2246400},"payments":{"status":"ok","rejected_24h":0,"stuck":0}}}`.
- `heartbeat` раз в минуту проверяет `/healthz` и отправляет POST на
  `HEARTBEAT_URL` с заголовками `X-Heartbeat-Token` и
  `Authorization: Bearer <token>`. Внутри общей Docker-сети обращайтесь к web
  по уникальному имени проекта (`http://club-web:8000/healthz`), а не по
  общему `web`: это имя может быть занято другим стеком на том же хосте.
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
