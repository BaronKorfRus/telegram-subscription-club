import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import texts
from app.bot.keyboards import tariff_keyboard
from app.config import settings
from app.db import SessionLocal
from app.models import Client, Subscription
from app.services import access, subscriptions

logger = logging.getLogger(__name__)

join_router = Router(name="join")


def _today() -> date:
    return datetime.now(ZoneInfo(settings.tz)).date()


def _latest_subscription(session: Session, tg_id: int) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .join(Client, Subscription.client_id == Client.id)
        .where(Client.tg_id == tg_id)
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
        .limit(1)
    )


def handle_join(session: Session, tg_id: int, today: date | None = None) -> bool:
    """Одобряет заявку только при действующем доступе. Телеграм вызывается здесь."""
    subscription = _latest_subscription(session, tg_id)
    if not subscriptions.has_access(subscription, today or _today()):
        return False
    return bool(access.approve_join(tg_id))


def _handle_join_in_thread(tg_id: int) -> bool:
    with SessionLocal() as session:
        return handle_join(session, tg_id)


@join_router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot) -> None:
    if settings.channel_id is not None and request.chat.id != settings.channel_id:
        logger.info(
            "chat_join_request decision=ignored tg_id=%s chat_id=%s",
            request.from_user.id,
            request.chat.id,
        )
        return

    approved = await asyncio.to_thread(_handle_join_in_thread, request.from_user.id)
    if approved:
        logger.info("chat_join_request decision=approved tg_id=%s", request.from_user.id)
        return

    logger.info("chat_join_request decision=not_paid tg_id=%s", request.from_user.id)
    try:
        await bot.send_message(
            request.user_chat_id,
            texts.JOIN_NOT_PAID,
            reply_markup=tariff_keyboard(),
        )
    except TelegramAPIError:
        # Пользователь мог не начинать чат с ботом; заявку всё равно отклоняем.
        logger.exception("Не удалось отправить отказ tg_id=%s", request.from_user.id)

    try:
        await request.decline()
    except TelegramAPIError:
        logger.exception("Не удалось отклонить заявку tg_id=%s", request.from_user.id)
    else:
        logger.info("chat_join_request decision=rejected tg_id=%s", request.from_user.id)
