from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from app import tbank, texts
from app.bot.handlers import registration
from app.config import settings
from app.models import Client, PaymentKind, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs
from app.services import card_change, payments, window
from app.services.payments import PaymentError
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
CLOSED_DAY = date(2026, 10, 26)
OPEN_DAY = date(2026, 10, 23)
TERMINAL_KEY = "TestTerminal"
PASSWORD = "test-password"
NOTIFICATION_URL = "https://callback.example/tbank/notify"


@pytest.fixture(autouse=True)
def tbank_settings(monkeypatch):
    monkeypatch.setattr(settings, "tbank_terminal_key", TERMINAL_KEY)
    monkeypatch.setattr(settings, "tbank_password", PASSWORD)
    monkeypatch.setattr(settings, "tbank_notification_url", NOTIFICATION_URL)


@pytest.fixture(autouse=True)
def clean_tables(session):
    session.execute(
        text("TRUNCATE payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()
    yield
    session.rollback()


@pytest.fixture
def tariff_m1(session):
    seed_tariffs(session)
    return session.query(Tariff).filter_by(code="m1").one()


@pytest.fixture
def client(session):
    row = Client(
        tg_id=920001, first_name="Анна", email="anna@example.com", phone="+79991234567"
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def closed_window():
    """«Сейчас» вне окна: 26 октября, оплата закрыта.

    Гейт читает момент из аргументов `today`/`now`, а не из wall-clock, поэтому
    фикстура только даёт дату — подменять часы не нужно.
    """
    return CLOSED_DAY


def _init_ok():
    return httpx.Response(
        200,
        json={
            "Success": True,
            "PaymentId": "777001",
            "PaymentURL": "https://pay.example/777001",
        },
    )


@respx.mock
def test_create_payment_outside_window_raises(session, client, tariff_m1, closed_window):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    with pytest.raises(payments.WindowClosedError) as excinfo:
        payments.create_tariff_payment(session, client, "m1", today=CLOSED_DAY)

    assert not route.called
    assert excinfo.value.window == window.Window(date(2026, 11, 22), date(2026, 11, 24))
    assert "22" in str(excinfo.value)


@respx.mock
def test_create_payment_inside_window_succeeds(session, client, tariff_m1):
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _url = payments.create_tariff_payment(session, client, "m1", today=OPEN_DAY)

    assert payment.kind == PaymentKind.initial


@respx.mock
def test_card_change_still_allowed_outside_window(session, client, tariff_m1, closed_window):
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())
    subscription = Subscription(
        client=client,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()

    payment, _url = card_change.start(session, client)

    assert payment.kind == PaymentKind.card_change
    assert payment.amount_kopecks == card_change.CARD_CHANGE_AMOUNT_KOPECKS


@respx.mock
def test_window_closed_error_is_payment_error(session, client, tariff_m1, closed_window):
    with pytest.raises(PaymentError):
        payments.create_payment(
            session,
            client,
            tariff_m1,
            PaymentKind.initial,
            now=datetime(2026, 10, 26, 12, tzinfo=MOSCOW),
        )


@respx.mock
def test_initial_payment_inside_window_uses_monthly_period(session, client, tariff_m1):
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _url = payments.create_tariff_payment(session, client, "m1", today=OPEN_DAY)

    assert payment.amount_kopecks == tariff_m1.price_kopecks


def test_is_payment_open_wrapper_matches_window(monkeypatch):
    assert payments.is_payment_open(datetime(2026, 11, 23, 12, tzinfo=MOSCOW))
    assert not payments.is_payment_open(datetime(2026, 11, 25, 12, tzinfo=MOSCOW))


class _FakeState:
    def __init__(self):
        self.current = "Registration:phone"

    async def get_data(self):
        return {"first_name": "Анна", "last_name": None, "email": "anna@example.com"}

    async def set_state(self, state):
        self.current = state

    async def clear(self):
        self.current = None


class _FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=920001, username="anna")
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


async def test_registration_outside_window_saves_client_and_shows_window_text(
    monkeypatch, session, closed_window
):
    saved = {}
    monkeypatch.setattr(
        registration, "_save_client", lambda tg_id, username, data, phone: saved.update(
            {"tg_id": tg_id, "phone": phone}
        )
    )
    monkeypatch.setattr(registration, "_payment_open", lambda: False)
    monkeypatch.setattr(
        registration, "_window_closed_text", lambda: texts.WINDOW_CLOSED.format(date="22 ноября")
    )
    message = _FakeMessage()
    state = _FakeState()

    await registration._finish_phone(message, state, "+79991234567")

    assert saved == {"tg_id": 920001, "phone": "+79991234567"}
    assert state.current is None
    assert "22" in message.answers[-1][0]
    texts_sent = [text for text, _markup in message.answers]
    assert texts.TARIFF_CHOICE not in texts_sent
    assert all(
        markup is None or not getattr(markup, "inline_keyboard", None)
        for _text, markup in message.answers
    )


async def test_registration_inside_window_shows_tariffs(monkeypatch, session):
    monkeypatch.setattr(registration, "_save_client", lambda *args: None)
    monkeypatch.setattr(registration, "_payment_open", lambda: True)
    message = _FakeMessage()
    state = _FakeState()

    await registration._finish_phone(message, state, "+79991234567")

    assert message.answers[-1][0] == texts.TARIFF_CHOICE
