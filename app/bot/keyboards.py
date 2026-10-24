from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app import texts

MENU_MY_SUBSCRIPTION = "Моя подписка"
MENU_SUBSCRIBE = "Оформить подписку"
MENU_QUESTIONS = "Вопросы"
MENU_RULES = "Правила"
MENU_SUPPORT = "Поддержка"
MENU_DESCRIPTION = "Описание"
MENU_LABELS = frozenset(
    {
        MENU_MY_SUBSCRIPTION,
        MENU_SUBSCRIBE,
        MENU_QUESTIONS,
        MENU_RULES,
        MENU_SUPPORT,
        MENU_DESCRIPTION,
    }
)

NEW_TARIFFS = (
    ("m1", "1 месяц"),
    ("m3", "3 месяца"),
    ("m6", "6 месяцев"),
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="menu:subscribe")],
            [InlineKeyboardButton(text="Моя подписка", callback_data="menu:manage")],
            [
                InlineKeyboardButton(text="FAQ", callback_data="menu:faq"),
                InlineKeyboardButton(text="Правила", callback_data="menu:rules"),
            ],
            [
                InlineKeyboardButton(text="Поддержка", callback_data="menu:support"),
                InlineKeyboardButton(text="Описание", callback_data="menu:description"),
            ],
        ]
    )


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_MY_SUBSCRIPTION), KeyboardButton(text=MENU_SUBSCRIBE)],
            [KeyboardButton(text=MENU_QUESTIONS), KeyboardButton(text=MENU_RULES)],
            [KeyboardButton(text=MENU_SUPPORT), KeyboardButton(text=MENU_DESCRIPTION)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="menu:subscribe")]
        ]
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def tariff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"tariff:{code}")]
            for code, title in NEW_TARIFFS
        ]
    )


def manage_keyboard(can_cancel: bool = True, can_change_card: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_cancel:
        rows.append([InlineKeyboardButton(text="Отменить подписку", callback_data="manage:cancel")])
    if can_change_card:
        rows.append(
            [InlineKeyboardButton(text="Заменить карту", callback_data="manage:change_card")]
        )
    rows.append([InlineKeyboardButton(text="Продлить", callback_data="manage:renew")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.CANCEL_YES, callback_data="manage:cancel:confirm"),
                InlineKeyboardButton(text=texts.CANCEL_NO, callback_data="manage:cancel:abort"),
            ]
        ]
    )


def payment_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить подписку", url=url)]]
    )


def card_change_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.CARD_CHANGE_BUTTON, url=url)]]
    )
