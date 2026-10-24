from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import texts
from app.bot.keyboards import (
    main_reply_keyboard,
    tariff_keyboard,
)
from app.bot.states import Registration
from app.config import settings
from app.db import SessionLocal
from app.models import Client, Subscription, SubscriptionStatus

start_router = Router(name="start")
start_router.message.filter(F.chat.type == "private")
start_router.callback_query.filter(F.message.chat.type == "private")

ACCESS_STATUSES = {
    SubscriptionStatus.active.value,
    SubscriptionStatus.past_due.value,
    SubscriptionStatus.cancelled.value,
}


def _today() -> date:
    return datetime.now(ZoneInfo(settings.tz)).date()


def _format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def subscription_text(
    status: SubscriptionStatus | str | None,
    current_period_end: date | None,
    today: date | None = None,
    name: str | None = None,
) -> str:
    """Выбирает текст статуса из блоков Salebot 49208034/49208035."""
    today = today or _today()
    status_value = status.value if isinstance(status, SubscriptionStatus) else str(status)
    if status_value == SubscriptionStatus.cancelled.value:
        if current_period_end is not None and current_period_end > today:
            return texts.SUBSCRIPTION_INACTIVE_UNTIL.format(date=_format_date(current_period_end))
        return texts.SUBSCRIPTION_ENDED.format(name=name or "Здравствуйте")
    if current_period_end is not None and current_period_end >= today:
        if status_value in ACCESS_STATUSES:
            return texts.SUBSCRIPTION_ACTIVE.format(date=_format_date(current_period_end))
        return texts.SUBSCRIPTION_INACTIVE_UNTIL.format(date=_format_date(current_period_end))
    return texts.SUBSCRIPTION_INACTIVE


def _client(session: Session, tg_id: int) -> Client | None:
    return session.scalar(select(Client).where(Client.tg_id == tg_id))


def _latest_subscription(session: Session, tg_id: int) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .join(Client)
        .where(Client.tg_id == tg_id)
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
        .limit(1)
    )


def _status_text(session: Session, tg_id: int) -> str:
    subscription = _latest_subscription(session, tg_id)
    if subscription is not None:
        client = subscription.client
        name = client.first_name or client.username or "Здравствуйте"
        return subscription_text(
            subscription.status,
            subscription.current_period_end,
            today=_today(),
            name=name,
        )
    if _client(session, tg_id) is not None:
        return subscription_text(None, None, today=_today())
    return texts.START


async def start_subscription(message: Message, state: FSMContext, tg_id: int) -> None:
    with SessionLocal() as session:
        client = _client(session, tg_id)
        has_contacts = bool(client is not None and client.email and client.phone)

    await state.clear()
    if has_contacts:
        await state.set_state(Registration.tariff)
        await message.answer(texts.TARIFF_CHOICE, reply_markup=tariff_keyboard())
    else:
        await state.set_state(Registration.name)
        await message.answer(texts.ASK_NAME)


@start_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    with SessionLocal() as session:
        text = _status_text(session, message.from_user.id)
    await message.answer(text, reply_markup=main_reply_keyboard())


@start_router.message(Command("description"))
async def cmd_description(message: Message) -> None:
    await message.answer(texts.DESCRIPTION)


@start_router.message(Command("questions"))
async def cmd_questions(message: Message) -> None:
    await message.answer(texts.FAQ)


@start_router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    await message.answer(texts.RULES)


@start_router.message(Command("technical_support"))
async def cmd_support(message: Message) -> None:
    await message.answer(texts.SUPPORT)


@start_router.callback_query(F.data == "menu:description")
async def cb_description(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(texts.DESCRIPTION)


@start_router.callback_query(F.data == "menu:faq")
async def cb_faq(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(texts.FAQ)


@start_router.callback_query(F.data == "menu:rules")
async def cb_rules(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(texts.RULES)


@start_router.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(texts.SUPPORT)


@start_router.callback_query(F.data == "menu:subscribe")
async def cb_subscribe(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await start_subscription(callback.message, state, callback.from_user.id)
