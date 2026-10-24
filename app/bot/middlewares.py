import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from sqlalchemy.orm import Session

from app.bot.keyboards import MENU_LABELS
from app.db import SessionLocal
from app.services import messages as messages_service

logger = logging.getLogger(__name__)


async def notify_admins(bot, session: Session, event: Message) -> None:
    """Сохраняет сообщение клиента, пересылает админам и шлёт служебный ответ."""
    if event.chat.type != "private":
        return

    client, saved, send_reply = messages_service.store_client_message(
        session,
        event.from_user.id,
        event.text,
        tg_message_id=event.message_id,
        username=event.from_user.username,
    )
    # Сообщение клиента сохраняем даже если Telegram недоступен.
    session.commit()

    admins = messages_service.active_admins(session)
    if admins and saved.tg_message_id:
        try:
            await messages_service.forward_to_admins(bot, session, client, saved, admins)
        except TelegramAPIError:
            logger.exception("Не удалось переслать сообщение админам")
    else:
        logger.warning("Нет активных админов для пересылки сообщения клиента")

    if send_reply:
        try:
            await bot.send_message(event.chat.id, messages_service.SERVICE_REPLY_TEXT)
        except TelegramAPIError:
            logger.exception("Не удалось отправить служебный ответ клиенту")


class PrivateChatMiddleware(BaseMiddleware):
    """Пропускает только личные чаты: группы и каналы молча игнорируются."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            if event.chat.type != "private":
                return None
        elif isinstance(event, CallbackQuery):
            if event.message is None or event.message.chat.type != "private":
                return None
        return await handler(event, data)


class OutOfFlowMiddleware(BaseMiddleware):
    """Перехватывает сообщения вне FSM: клиентские — админам, админские — клиенту."""

    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)
        if event.chat.type != "private":
            return None

        state = data.get("state")
        if state is not None and await state.get_state() is not None:
            return await handler(event, data)

        text = (event.text or "").strip()
        if text.startswith("/") or text.lower() == "admin" or text in MENU_LABELS:
            return await handler(event, data)

        bot = data["bot"]
        with SessionLocal() as session:
            if messages_service.is_admin(session, event.from_user.id):
                await self._handle_admin(event, session, bot)
            else:
                await self._handle_client(event, session, bot)
            session.commit()
        return None

    async def _handle_client(self, event: Message, session: Session, bot) -> None:
        await notify_admins(bot, session, event)

    async def _handle_admin(self, event: Message, session: Session, bot) -> None:
        reply = event.reply_to_message
        if reply is not None and event.text:
            client_tg_id = messages_service.handle_admin_reply(
                session,
                event.from_user.id,
                reply.message_id,
                event.text,
            )
            session.commit()
            if client_tg_id is not None:
                try:
                    await bot.send_message(client_tg_id, event.text)
                except TelegramAPIError:
                    logger.warning("Не удалось отправить ответ админа клиенту")
                return
        try:
            await bot.send_message(event.from_user.id, messages_service.ADMIN_REPLY_HINT)
        except TelegramAPIError:
            # Владелец/админ мог не начинать чат с ботом; update не валим.
            logger.warning("Не удалось отправить подсказку админу tg_id=%s", event.from_user.id)
