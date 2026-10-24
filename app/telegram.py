import httpx

from app.config import settings

BASE_URL = "https://api.telegram.org"
TIMEOUT_SECONDS = 20.0


class TelegramError(RuntimeError):
    """Telegram ответил ошибкой, 5xx или произошёл сетевой сбой."""


def call(method: str, params: dict) -> object:
    """Вызов метода Bot API. Возвращает поле result из ответа."""
    if not settings.bot_token:
        raise TelegramError("BOT_TOKEN не задан")

    url = f"{BASE_URL}/bot{settings.bot_token}/{method}"
    kwargs: dict = {"json": params, "timeout": TIMEOUT_SECONDS}
    if settings.telegram_proxy:
        kwargs["proxy"] = settings.telegram_proxy

    try:
        response = httpx.post(url, **kwargs)
    except httpx.HTTPError:
        # Причину не прикладываем: в URL запроса находится токен бота.
        raise TelegramError(f"{method}: сетевой сбой") from None

    if response.status_code >= 500:
        raise TelegramError(f"{method}: HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"{method}: некорректный ответ Telegram") from None

    if not isinstance(data, dict):
        raise TelegramError(f"{method}: неожиданный формат ответа Telegram")
    if not data.get("ok"):
        description = data.get("description") or "Telegram API error"
        raise TelegramError(f"{method}: {description}")
    return data.get("result")
