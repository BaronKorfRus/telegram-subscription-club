import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import texts
from app.bot.handlers.start import subscription_text
from app.bot.keyboards import (
    cancel_confirm_keyboard,
    card_change_keyboard,
    main_menu_keyboard,
    manage_keyboard,
    payment_link_keyboard,
    subscribe_keyboard,
)
from app.config import settings
from app.db import SessionLocal
from app.models import Client, Job, JobType, Subscription, SubscriptionStatus
from app.services import card_change, jobs, payments, subscriptions

logger = logging.getLogger(__name__)

manage_router = Router(name="manage")
manage_router.message.filter(F.chat.type == "private")
manage_router.callback_query.filter(F.message.chat.type == "private")


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _display_name(subscription: Subscription) -> str:
    client = subscription.client
    return client.first_name or client.username or "Здравствуйте"


def status_message(subscription: Subscription, today: date | None = None) -> str:
    today = today or _now().date()
    if subscription.status == SubscriptionStatus.cancelled and not subscription.auto_renew:
        if subscription.current_period_end > today:
            text = texts.CANCEL_DONE.format(date=_format_date(subscription.current_period_end))
            if subscription.tariff is not None:
                text += f"\n\nТариф: {subscription.tariff.title}"
            return text
        return texts.SUBSCRIPTION_ENDED.format(name=_display_name(subscription))

    text = subscription_text(subscription.status, subscription.current_period_end, today=today)
    if subscription.tariff is not None:
        text += f"\nТариф: {subscription.tariff.title}"
        price_rub = subscription.tariff.price_kopecks // 100
        text += f"\nСумма: {price_rub} ₽"
        text += f"\nДата следующего списания: {_format_date(subscription.current_period_end)}"
    return text


def status_view(
    subscription: Subscription, today: date | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура статуса; после окончания срока — кнопка оплаты."""
    today = today or _now().date()
    text = status_message(subscription, today=today)
    ended = (
        subscription.status == SubscriptionStatus.cancelled
        and not subscription.auto_renew
        and subscription.current_period_end <= today
    )
    if ended:
        return text, subscribe_keyboard()
    return text, manage_keyboard(
        can_cancel=subscription.auto_renew,
        can_change_card=subscription.auto_renew,
    )


def cancel_subscription(session: Session, subscription: Subscription, now: datetime) -> Job | None:
    """Отмена + задача kick на 09:00 МСК в день окончания периода."""
    already_cancelled = (
        subscription.status == SubscriptionStatus.cancelled and not subscription.auto_renew
    )
    subscriptions.cancel(session, subscription, now)
    if already_cancelled:
        return None

    run_at = datetime.combine(
        subscription.current_period_end,
        time(hour=9),
        tzinfo=ZoneInfo(settings.tz),
    )
    return jobs.enqueue(
        session,
        JobType.kick.value,
        run_at,
        {"subscription_id": subscription.id, "tg_id": subscription.client.tg_id},
    )


def _client(session: Session, tg_id: int) -> Client | None:
    return session.scalar(select(Client).where(Client.tg_id == tg_id))


def _latest_subscription(session: Session, client: Client) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .where(Subscription.client_id == client.id)
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
        .limit(1)
    )


async def show_status(message: Message, tg_id: int) -> None:
    with SessionLocal() as session:
        client = _client(session, tg_id)
        subscription = payments.open_subscription(session, client) if client else None
        if subscription is None and client is not None:
            subscription = _latest_subscription(session, client)
        if subscription is None:
            text = texts.SUBSCRIPTION_INACTIVE
            markup = subscribe_keyboard()
        else:
            text, markup = status_view(subscription)
    await message.answer(text, reply_markup=markup)


@manage_router.message(Command("manage_sub"))
async def cmd_manage_sub(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is not None:
        await show_status(message, message.from_user.id)


@manage_router.callback_query(F.data == "menu:manage")
async def cb_manage(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_status(callback.message, callback.from_user.id)


@manage_router.callback_query(F.data == "manage:cancel")
async def cb_cancel(callback: CallbackQuery) -> None:
    with SessionLocal() as session:
        client = _client(session, callback.from_user.id)
        subscription = payments.open_subscription(session, client) if client else None
        if subscription is None:
            await callback.answer("Нет активной подписки", show_alert=True)
            return
        text = texts.CANCEL_CONFIRM.format(date=_format_date(subscription.current_period_end))
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=cancel_confirm_keyboard())


@manage_router.callback_query(F.data == "manage:cancel:confirm")
async def cb_cancel_confirm(callback: CallbackQuery) -> None:
    with SessionLocal() as session:
        client = _client(session, callback.from_user.id)
        subscription = payments.open_subscription(session, client) if client else None
        if subscription is None:
            await callback.answer("Нет активной подписки", show_alert=True)
            return
        period_end = subscription.current_period_end
        cancel_subscription(session, subscription, _now())
        session.commit()
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            texts.CANCEL_DONE.format(date=_format_date(period_end)),
            reply_markup=main_menu_keyboard(),
        )


@manage_router.callback_query(F.data == "manage:cancel:abort")
async def cb_cancel_abort(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_status(callback.message, callback.from_user.id)


@manage_router.callback_query(F.data == "manage:change_card")
async def cb_change_card(callback: CallbackQuery) -> None:
    with SessionLocal() as session:
        client = _client(session, callback.from_user.id)
        if client is None:
            await callback.answer("Сначала пройдите регистрацию", show_alert=True)
            return
        try:
            _payment, payment_url = card_change.start(session, client)
        except payments.PaymentError:
            logger.exception("Не удалось создать платёж за замену карты tg_id=%s", client.tg_id)
            await callback.answer("Не удалось создать платёж", show_alert=True)
            return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            texts.CARD_CHANGE, reply_markup=card_change_keyboard(payment_url)
        )


@manage_router.callback_query(F.data == "manage:renew")
async def cb_renew(callback: CallbackQuery) -> None:
    with SessionLocal() as session:
        client = _client(session, callback.from_user.id)
        subscription = payments.open_subscription(session, client) if client else None
        if subscription is None or subscription.tariff is None:
            await callback.answer("Нет активной подписки", show_alert=True)
            return
        try:
            _payment, payment_url = payments.create_tariff_payment(
                session, client, subscription.tariff.code
            )
        except payments.PaymentError:
            logger.exception("Не удалось продлить подписку tg_id=%s", client.tg_id)
            await callback.answer("Не удалось создать платёж", show_alert=True)
            return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "Оплатить подписку", reply_markup=payment_link_keyboard(payment_url)
        )
