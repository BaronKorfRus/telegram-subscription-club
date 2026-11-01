# Внешний сторож (Amnezia Backup)

Сторож живёт вне основного сервера, поэтому падение сервера его не
касается: пока сервер жив, `heartbeat` раз в минуту проверяет локальный
`/healthz` и отправляет POST на `HEARTBEAT_URL`.

Контракт хартбита:

- `POST $HEARTBEAT_URL`
- заголовки: `X-Heartbeat-Token: $HEARTBEAT_TOKEN` и
  `Authorization: Bearer $HEARTBEAT_TOKEN`
- тело: `{"service": "club"}`
- ожидаемый ответ: 2xx

Если стука нет 3 минуты, сторож пишет в Telegram админам. Сторож не должен
лежать в этом compose-стеке и не должен зависеть от базы клуба.

Сервис `heartbeat` в `docker-compose.yml` запускает `python -m app.heartbeat`
и проверяет `http://web:8000/healthz`. Если локальный healthz не 200, хартбит
не отправляется — для сторожа это сигнал тревоги.
