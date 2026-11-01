import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers.admin import admin_router
from app.bot.handlers.join import join_router
from app.bot.handlers.manage import manage_router
from app.bot.handlers.menu import menu_router
from app.bot.handlers.payment import payment_router
from app.bot.handlers.registration import registration_router
from app.bot.handlers.start import start_router
from app.bot.middlewares import OutOfFlowMiddleware, PrivateChatMiddleware
from app.config import settings
from app.db import SessionLocal
from app.services import health
from app.services.messages import ensure_owner


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(PrivateChatMiddleware())
    dispatcher.callback_query.outer_middleware(PrivateChatMiddleware())
    dispatcher.message.outer_middleware(OutOfFlowMiddleware())
    dispatcher.include_router(menu_router)
    dispatcher.include_router(start_router)
    dispatcher.include_router(registration_router)
    dispatcher.include_router(payment_router)
    dispatcher.include_router(manage_router)
    dispatcher.include_router(join_router)
    dispatcher.include_router(admin_router)
    dispatcher.startup.register(_on_startup)
    return dispatcher


async def _on_startup() -> None:
    with SessionLocal() as session:
        ensure_owner(session)
        session.commit()


async def _heartbeat_loop() -> None:
    """Отметка живости для /healthz: пишется из самого процесса бота."""
    logger = logging.getLogger(__name__)
    while True:
        try:
            await asyncio.to_thread(health.beat_own_session, health.BOT)
        except Exception:
            logger.exception("Не удалось записать отметку живости бота")
        await asyncio.sleep(health.BEAT_INTERVAL_SECONDS)


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    asyncio.create_task(_heartbeat_loop())
    await create_dispatcher().start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
