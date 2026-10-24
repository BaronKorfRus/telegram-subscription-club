import re
from datetime import date
from types import SimpleNamespace

import pytest
from app import texts
from app.bot.handlers import registration
from app.bot.handlers.registration import (
    is_valid_email,
    is_valid_phone,
    normalize_phone,
    split_name,
)
from app.bot.handlers.start import subscription_text
from app.bot.keyboards import tariff_keyboard
from app.bot.states import Registration
from app.models import SubscriptionStatus


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("a@b.ru", True),
        ("user.name+tag@sub.domain.ru", True),
        ("a@b", False),
        ("", False),
    ],
)
def test_is_valid_email(value, ok):
    assert is_valid_email(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("+79991234567", True),
        ("+7 999 123 45 67", True),
        ("8 (999) 123-45-67", True),
        ("123", False),
        ("", False),
    ],
)
def test_is_valid_phone(value, ok):
    assert is_valid_phone(value) is ok


def test_phone_is_normalized_to_plus_seven():
    assert normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert normalize_phone("+79991234567") == "+79991234567"


def test_split_name():
    assert split_name("Анна Смирнова") == ("Анна", "Смирнова")
    assert split_name("Анна") == ("Анна", None)


def test_status_text_active():
    text = subscription_text(
        SubscriptionStatus.active, date(2026, 11, 22), today=date(2026, 10, 23)
    )
    assert "Статус вашей подписки: Активна до 22.11.2026" in text
    assert "Отменить подписку" in text


def test_status_text_past_due_keeps_access_until_period_end():
    text = subscription_text(
        SubscriptionStatus.past_due, date(2026, 11, 22), today=date(2026, 10, 23)
    )
    assert "Активна до 22.11.2026" in text


def test_status_text_inactive_without_future_date():
    assert (
        subscription_text(SubscriptionStatus.expired, date(2026, 9, 1), today=date(2026, 10, 23))
        == texts.SUBSCRIPTION_INACTIVE
    )
    assert subscription_text(None, None, today=date(2026, 10, 23)) == texts.SUBSCRIPTION_INACTIVE


def test_status_text_inactive_until_future_date():
    text = subscription_text(
        SubscriptionStatus.expired, date(2026, 11, 1), today=date(2026, 10, 23)
    )
    assert text == texts.SUBSCRIPTION_INACTIVE_UNTIL.format(date="01.11.2026")


def test_fsm_state_names():
    assert Registration.name.state == "Registration:name"
    assert Registration.email.state == "Registration:email"
    assert Registration.phone.state == "Registration:phone"
    assert Registration.tariff.state == "Registration:tariff"


def test_tariff_keyboard_callbacks():
    keyboard = tariff_keyboard()
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["tariff:m1", "tariff:m3", "tariff:m6"]


def test_status_text_cancelled_tomorrow_keeps_access_text():
    text = subscription_text(
        SubscriptionStatus.cancelled,
        date(2026, 10, 24),
        today=date(2026, 10, 23),
        name="Анна",
    )

    assert text == texts.SUBSCRIPTION_INACTIVE_UNTIL.format(date="24.10.2026")


def test_status_text_cancelled_today_is_ended():
    text = subscription_text(
        SubscriptionStatus.cancelled,
        date(2026, 10, 23),
        today=date(2026, 10, 23),
        name="Анна",
    )

    assert text == texts.SUBSCRIPTION_ENDED.format(name="Анна")
    assert "подписка окончена" in text


def test_status_text_cancelled_yesterday_is_ended():
    text = subscription_text(
        SubscriptionStatus.cancelled,
        date(2026, 10, 22),
        today=date(2026, 10, 23),
        name="Анна",
    )

    assert text == texts.SUBSCRIPTION_ENDED.format(name="Анна")


def test_support_text_mentions_writing_in_bot_without_link():
    assert (
        "Можете написать свой вопрос прямо здесь, в боте — сообщение уйдёт администратору"
        in texts.SUPPORT
    )
    assert not re.search(r"t\.me/|@\w+", texts.SUPPORT)
    assert "https://t.me/" not in texts.SUPPORT


class _FakeState:
    def __init__(self):
        self.current = None

    async def get_data(self):
        return {"first_name": "Анна", "last_name": None, "email": "anna@example.com"}

    async def set_state(self, state):
        self.current = state


class _FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=970001, username="anna")
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


async def test_finish_phone_shows_persistent_menu(monkeypatch):
    monkeypatch.setattr(registration, "_save_client", lambda *args: None)
    message = _FakeMessage()
    state = _FakeState()

    await registration._finish_phone(message, state, "+79991234567")

    assert message.answers[0][0] == texts.MENU_HINT
    assert message.answers[0][1].is_persistent is True
    assert message.answers[0][1].resize_keyboard is True
    assert message.answers[1][0] == texts.TARIFF_CHOICE
