import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app import texts
from app.db import SessionLocal
from app.models import Admin, AdminStatus
from app.services import messages as messages_service

logger = logging.getLogger(__name__)

admin_router = Router(name="admin")
admin_router.message.filter(F.chat.type == "private")
admin_router.callback_query.filter(F.message.chat.type == "private")


def _is_admin_word(message: Message) -> bool:
    return bool(message.text and message.text.strip().lower() == "admin")


def _full_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        return ""
    return " ".join(filter(None, [user.first_name, user.last_name])) or user.username or ""


def _request_card(message: Message) -> str:
    user = message.from_user
    username = f"@{user.username}" if user is not None and user.username else "—"
    return (
        "Заявка на права администратора\n"
        f"Имя: {_full_name(message)}\n"
        f"Username: {username}\n"
        f"ID: {user.id if user else '—'}"
    )


def _request_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=f"admin:confirm:{tg_id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"admin:reject:{tg_id}"),
            ]
        ]
    )


@admin_router.message(_is_admin_word)
async def on_admin_word(message: Message) -> None:
    if message.from_user is None:
        return
    with SessionLocal() as session:
        if messages_service.is_admin(session, message.from_user.id):
            await message.answer(texts.ADMIN_ALREADY)
            return

        existing = session.scalar(select(Admin).where(Admin.tg_id == message.from_user.id))
        if existing is not None and existing.status == AdminStatus.pending:
            await message.answer(texts.ADMIN_REQUEST_SENT)
            return

        request = messages_service.handle_admin_word(
            session, message.from_user.id, name=_full_name(message) or None
        )
        owners = [a for a in messages_service.active_admins(session) if a.is_owner]
        session.commit()
        tg_id = request.tg_id

    markup = _request_keyboard(tg_id)
    card = _request_card(message)
    sent = await messages_service.send_to_admins(message.bot, owners, card, reply_markup=markup)
    if sent == 0:
        logger.error("Не удалось отправить заявку ни одному владельцу tg_id=%s", tg_id)
    await message.answer(texts.ADMIN_REQUEST_SENT)


async def _owner_callback(session, callback: CallbackQuery) -> bool:
    if not messages_service.is_owner(session, callback.from_user.id):
        await callback.answer("Только владелец может подтверждать", show_alert=True)
        return False
    return True


@admin_router.callback_query(F.data.startswith("admin:confirm:"))
async def cb_confirm_admin(callback: CallbackQuery) -> None:
    try:
        target_tg_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    with SessionLocal() as session:
        if not await _owner_callback(session, callback):
            return
        target = messages_service.confirm_admin(
            session, target_tg_id, added_by=callback.from_user.id
        )
        if target is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        admins = messages_service.active_admins(session)
        target_name = target.name or str(target.tg_id)
        session.commit()

    await callback.answer("Подтверждено")
    await messages_service.send_to_admins(
        callback.bot, admins, f"Новый администратор: {target_name} ({target_tg_id})"
    )
    if callback.message is not None:
        await callback.message.edit_text(f"Администратор {target_name} подтверждён")


@admin_router.callback_query(F.data.startswith("admin:reject:"))
async def cb_reject_admin(callback: CallbackQuery) -> None:
    try:
        target_tg_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    with SessionLocal() as session:
        if not await _owner_callback(session, callback):
            return
        target = messages_service.reject_admin(session, target_tg_id)
        if target is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        target_name = target.name or str(target.tg_id)
        session.commit()

    await callback.answer("Отклонено")
    try:
        await callback.bot.send_message(target_tg_id, texts.ADMIN_REJECTED)
    except TelegramAPIError:
        logger.exception("Не удалось уведомить заявителя tg_id=%s", target_tg_id)
    if callback.message is not None:
        await callback.message.edit_text(f"Заявка {target_name} отклонена")
