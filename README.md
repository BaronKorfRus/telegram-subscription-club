# Telegram Subscription Club

Бот для закрытого Telegram-клуба: регистрация, оплата через Т-Банк,
автоматическое продление подписки и управление доступом в канал и чат.

Проект можно использовать как основу для своего клуба: тексты, тарифы и бренд
вынесены в контент-слой, а код не содержит названий, доменов и ID конкретного
сообщества. В репозитории лежит вымышленный пример — «Клуб».

## Что умеет

- Регистрация в боте: имя, email и телефон для чека.
- Оплата подписки через Т-Банк: первичный платёж, рекуррентные списания,
  замена карты.
- Автопродление: напоминания за 3 и 1 день, списание в день окончания.
- Понятная работа с отказами: повторные попытки, пауза, истечение подписки.
- Доступ в канал и чат: выдача после оплаты, удаление после отмены или
  истечения срока.
- Ответы администратора: сообщение клиента пересылается админам, ответ
  возвращается клиенту.
- Ежедневная сводка и внешний хартбит.

## Преимущества

- **Self-hosted.** База, бот и платежи живут на вашем сервере.
- **Контент отдельно от кода.** Тексты и тарифы — YAML-файлы; код не нужно
  менять при смене названия, цены или формулировки.
- **Идемпотентность.** Повторное уведомление банка не продлевает подписку
  второй раз.
- **Аккуратный дуннинг.** Сетевой сбой не считается отказом карты; отказ
  карты ведёт к предсказуемым повторным попыткам.
- **Прозрачные платежи.** События банка сохраняются, суммы сверяются.
- **Готовый переезд.** Есть миграция клиентов из Salebot по API.
- **MIT.** Можно использовать, менять и развивать.

## Архитектура

```mermaid
flowchart LR
    U[Клиент в Telegram] --> B[bot: aiogram]
    B --> DB[(PostgreSQL)]
    W[web: FastAPI] --> DB
    B --> TB[Т-Банк]
    TB -->|HTTP-уведомления| W
    W --> TB
    J[billing: worker] --> DB
    J --> TB
    J --> TG[Telegram API]
    B --> TG
    H[heartbeat] --> W
```

Подробнее — [docs/architecture.md](docs/architecture.md).

## Быстрый старт

Нужны Docker, домен и заполненный `.env`.

```bash
cp .env.example .env
cp -r content/example content
# заполните BOT_TOKEN, ID канала и чата, ключи Т-Банка и домен;
# CONTENT_DIR_HOST=./content уже указан в .env.example
#
# compose сам монтирует ${CONTENT_DIR_HOST:-./content}:/content:ro
# и задаёт сервисам CONTENT_DIR=/content — override не нужен

# общая сеть для Caddy; если Caddy ещё нет, создайте сеть
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
docker compose exec web python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status)"
```

Если Telegram доступен напрямую, оставьте `TELEGRAM_PROXY` пустым и не
запускайте `tgproxy` (как в команде выше). Если прокси нужен, подготовьте
ключи и `known_hosts` и запустите `docker compose up -d` — см.
[docs/self-hosting.md](docs/self-hosting.md).

Для локальной разработки:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/club_test
pytest -q
ruff check .
```

## Контент-слой

Тексты и тарифы читаются из каталога `CONTENT_DIR`:

- `texts.yaml` — все сообщения бота, кнопки и шаблоны;
- `tariffs.yaml` — коды, названия, цены в копейках, сроки и флаги.

В репозитории используется `content/example`. Скопируйте его в `content/`,
замените тексты и тарифы на свои и укажите в `.env`
`CONTENT_DIR_HOST=./content`: `docker-compose.yml` смонтирует этот каталог в
`/content`. Код при этом менять не нужно. После изменения тарифов загрузите
новые коды в базу — команда есть в
[docs/self-hosting.md](docs/self-hosting.md).

## Т-Банк

Поддерживаются `Init`, `Charge`, `GetState`, `Cancel`, рекуррентные списания,
чеки и HTTP-уведомления. Подпись проверяется, события сохраняются, суммы
сверяются. Ключи терминала живут только в `.env`.

Подробнее — [docs/tbank.md](docs/tbank.md).

## Переезд с Salebot

Три пути описаны в [docs/salebot-migration.md](docs/salebot-migration.md):

1. **API Salebot** — выгрузка клиентов через `get_clients` и `get_variables`
   и маппинг в нашу схему.
2. **Бэкап схемы `.sbk`** — как читать блоки, условия, калькулятор и таймеры;
   это образец логики, а не файл для импорта.
3. **Текущая реализация** — те же шаги в коде этого проекта.

## Self-hosting

Docker, `.env`, контент, Caddy, бэкап, мониторинг и обновления —
[docs/self-hosting.md](docs/self-hosting.md).

## Безопасность

Секреты, подпись уведомлений, доступы и работа с персональными данными —
[docs/security.md](docs/security.md). Как сообщить об уязвимости —
[SECURITY.md](SECURITY.md).

## Участие

Правила разработки и тестов — [CONTRIBUTING.md](CONTRIBUTING.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).

## Ограничения

- Один клуб на инстанс; мультитенантности нет.
- Поддерживается Т-Банк; другие платёжные провайдеры не реализованы.
- Telegram — единственный интерфейс; веб-кабинета нет.
- Контент не встроен в Docker-образ: `docker-compose.yml` монтирует каталог
  из `CONTENT_DIR_HOST` в `/content`.
- Для тестов нужна отдельная PostgreSQL.
- Проект не заменяет юридическую и бухгалтерскую проверку: чеки, налоги и
  согласия остаются ответственностью владельца клуба.
