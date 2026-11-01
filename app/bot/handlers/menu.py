from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.bot.handlers.manage import show_status
from app.bot.handlers.start import start_subscription
from app.bot.keyboards import (
    MENU_DESCRIPTION,
    MENU_LABELS,
    MENU_MY_SUBSCRIPTION,
    MENU_QUESTIONS,
    MENU_RULES,
    MENU_SUBSCRIBE,
    MENU_SUPPORT,
    main_reply_keyboard,
)

menu_router = Router(name="menu")
menu_router.message.filter(F.chat.type == "private")


@menu_router.message(F.text.in_(MENU_LABELS))
async def on_menu_button(message: Message, state: FSMContext) -> None:
    """Кнопки нижнего меню работают как команды и сбрасывают шаг регистрации."""
    await state.clear()
    if message.from_user is None:
        return

    label = (message.text or "").strip()
    if label == MENU_MY_SUBSCRIPTION:
        await show_status(message, message.from_user.id)
    elif label == MENU_SUBSCRIBE:
        await start_subscription(message, state, message.from_user.id)
    elif label == MENU_QUESTIONS:
        await message.answer(texts.FAQ, reply_markup=main_reply_keyboard())
    elif label == MENU_RULES:
        await message.answer(texts.RULES, reply_markup=main_reply_keyboard())
    elif label == MENU_SUPPORT:
        await message.answer(texts.SUPPORT)
    elif label == MENU_DESCRIPTION:
        await message.answer(texts.DESCRIPTION, reply_markup=main_reply_keyboard())
