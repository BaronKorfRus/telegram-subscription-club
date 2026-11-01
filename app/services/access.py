import logging

from app import telegram
from app.config import settings

logger = logging.getLogger(__name__)


class AccessError(RuntimeError):
    """Не удалось изменить доступ в канале/чате; задачу нужно повторить."""


def _chat_ids() -> tuple[int, int]:
    if settings.channel_id is None or settings.chat_id is None:
        raise AccessError("CHANNEL_ID и CHAT_ID должны быть заданы")
    return int(settings.channel_id), int(settings.chat_id)


def _call(method: str, params: dict, tg_id: int) -> object:
    try:
        return telegram.call(method, params)
    except telegram.TelegramError as exc:
        logger.error("Telegram %s failed for tg_id=%s: %s", method, tg_id, exc)
        raise AccessError(f"{method}: {exc}") from exc


def grant(tg_id: int) -> None:
    """Разбанить пользователя в канале и чате, чтобы он мог войти по ссылке."""
    for chat_id in _chat_ids():
        _call(
            "unbanChatMember",
            {"chat_id": chat_id, "user_id": tg_id, "only_if_banned": True},
            tg_id,
        )


def revoke(tg_id: int) -> None:
    """Забанить (удалить) пользователя из канала и чата."""
    for chat_id in _chat_ids():
        _call("banChatMember", {"chat_id": chat_id, "user_id": tg_id}, tg_id)


def approve_join(tg_id: int) -> bool:
    """Одобрить заявку на вступление в канал."""
    if settings.channel_id is None:
        raise AccessError("CHANNEL_ID должен быть задан")
    result = _call(
        "approveChatJoinRequest",
        {"chat_id": int(settings.channel_id), "user_id": tg_id},
        tg_id,
    )
    return bool(result)
