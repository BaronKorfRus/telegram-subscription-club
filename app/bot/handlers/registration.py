import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from app import texts
from app.bot.keyboards import main_reply_keyboard, phone_keyboard, tariff_keyboard
from app.bot.states import Registration
from app.db import SessionLocal
from app.models import Client

registration_router = Router(name="registration")
registration_router.message.filter(F.chat.type == "private")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9][-A-Za-z0-9]*\.)+[A-Za-z]{2,}$")
_PHONE_CLEAN_RE = re.compile(r"[\s()\-]")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(value.strip()))


def normalize_phone(value: str) -> str | None:
    cleaned = _PHONE_CLEAN_RE.sub("", value.strip())
    if re.fullmatch(r"\+7\d{10}", cleaned):
        return cleaned
    if re.fullmatch(r"[78]\d{10}", cleaned):
        return "+7" + cleaned[1:]
    return None


def is_valid_phone(value: str) -> bool:
    return normalize_phone(value) is not None


def split_name(value: str) -> tuple[str, str | None]:
    parts = value.strip().split(maxsplit=1)
    if not parts:
        return "", None
    return parts[0], parts[1] if len(parts) > 1 else None


def _save_client(tg_id: int, username: str | None, data: dict, phone: str) -> None:
    with SessionLocal() as session:
        client = session.scalar(select(Client).where(Client.tg_id == tg_id))
        if client is None:
            client = Client(tg_id=tg_id)
            session.add(client)
        client.username = username
        client.first_name = data.get("first_name")
        client.last_name = data.get("last_name")
        client.email = data.get("email")
        client.phone = phone
        session.commit()


async def _finish_phone(message: Message, state: FSMContext, phone: str) -> None:
    user = message.from_user
    if user is None:
        return
    data = await state.get_data()
    _save_client(user.id, user.username, data, phone)
    await state.set_state(Registration.tariff)
    await message.answer(texts.MENU_HINT, reply_markup=main_reply_keyboard())
    await message.answer(texts.TARIFF_CHOICE, reply_markup=tariff_keyboard())


@registration_router.message(Registration.name)
async def process_name(message: Message, state: FSMContext) -> None:
    first_name, last_name = split_name(message.text or "")
    if not first_name:
        await message.answer(texts.ASK_NAME)
        return
    await state.update_data(first_name=first_name, last_name=last_name)
    await state.set_state(Registration.email)
    await message.answer(texts.ASK_EMAIL)


@registration_router.message(Registration.email)
async def process_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip().lower()
    if not is_valid_email(email):
        await message.answer(texts.INVALID_EMAIL)
        return
    await state.update_data(email=email)
    await state.set_state(Registration.phone)
    await message.answer(texts.ASK_PHONE, reply_markup=phone_keyboard())


@registration_router.message(Registration.phone, F.contact)
async def process_contact(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.contact.phone_number) if message.contact else None
    if phone is None:
        await message.answer(texts.INVALID_PHONE)
        return
    await _finish_phone(message, state, phone)


@registration_router.message(Registration.phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer(texts.INVALID_PHONE)
        return
    await _finish_phone(message, state, phone)
