import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Client
from app.services import payments

logger = logging.getLogger(__name__)

payment_router = Router(name="payment")
payment_router.callback_query.filter(F.message.chat.type == "private")


@payment_router.callback_query(F.data.startswith("tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    code = (callback.data or "").split(":", 1)[-1]
    with SessionLocal() as session:
        client = session.scalar(select(Client).where(Client.tg_id == callback.from_user.id))
        if client is None:
            await callback.answer("Сначала пройдите регистрацию", show_alert=True)
            return

        allowed = payments.offered_tariffs(session, client)
        if not any(tariff.code == code for tariff in allowed):
            await callback.answer("Этот тариф недоступен", show_alert=True)
            return

        try:
            _payment, payment_url = payments.create_tariff_payment(session, client, code)
        except payments.PaymentError:
            logger.exception("Не удалось создать платёж для tg_id=%s", client.tg_id)
            await callback.answer("Не удалось создать платёж, попробуйте ещё раз", show_alert=True)
            return

    await state.clear()
    await callback.answer()
    if callback.message is None:
        return

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить подписку", url=payment_url)]]
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        logger.exception("Не удалось заменить клавиатуру на ссылку оплаты")
        await callback.message.answer("Оплатить подписку", reply_markup=markup)
