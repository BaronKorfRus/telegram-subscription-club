import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from app import tbank
from app.bot.handlers.manage import cancel_subscription, status_message, status_view
from app.config import settings
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import card_change, jobs, subscriptions
from app.services.payments import (
    PaymentError,
    create_tariff_payment,
    handle_notification,
)
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 23)
PERIOD_END = date(2026, 11, 22)
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
TERMINAL_KEY = "TestTerminal"
PASSWORD = "test-password"
NOTIFICATION_URL = "https://callback.example/tbank/notify"


@pytest.fixture(autouse=True)
def tbank_settings(monkeypatch):
    monkeypatch.setattr(settings, "tbank_terminal_key", TERMINAL_KEY)
    monkeypatch.setattr(settings, "tbank_password", PASSWORD)
    monkeypatch.setattr(settings, "tbank_notification_url", NOTIFICATION_URL)


@pytest.fixture(autouse=True)
def clean_manage_tables(session):
    _truncate(session)
    yield
    session.rollback()
    _truncate(session)


def _truncate(session):
    session.execute(
        text("TRUNCATE jobs, payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()


@pytest.fixture
def active_sub(session):
    seed_tariffs(session)
    client = Client(
        tg_id=920001,
        first_name="Анна",
        email="anna@example.com",
        phone="+79991234567",
    )
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=PERIOD_END,
        auto_renew=True,
        rebill_id="old-rebill",
        card_mask="430000******0000",
    )
    session.add(subscription)
    session.flush()
    return subscription


def _init_ok():
    return httpx.Response(
        200,
        json={
            "Success": True,
            "PaymentId": "888001",
            "PaymentURL": "https://pay.example/888001",
        },
    )


def test_cancel_keeps_access_until_period_end(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)

    assert active_sub.auto_renew is False
    assert active_sub.rebill_id is None
    assert active_sub.current_period_end == PERIOD_END
    assert active_sub.status == SubscriptionStatus.cancelled


def test_cancel_schedules_kick_at_nine_moscow(session, active_sub):
    job = cancel_subscription(session, active_sub, NOW)

    assert job is not None
    assert job.type == JobType.kick
    assert job.status == JobStatus.pending
    assert job.run_at == datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)
    assert job.payload == {"subscription_id": active_sub.id, "tg_id": 920001}


def test_cancel_removes_charge_and_reminders_but_keeps_kick(session, active_sub):
    for job_type in (JobType.charge, JobType.reminder_3d, JobType.reminder_1d):
        jobs.enqueue(session, job_type.value, NOW, {"subscription_id": active_sub.id})
    session.flush()

    kick = cancel_subscription(session, active_sub, NOW)

    remaining = session.query(Job).all()
    assert kick is not None
    assert {job.type for job in remaining} == {JobType.kick}
    assert remaining[0].id == kick.id


def test_cancel_twice_does_not_duplicate_kick(session, active_sub):
    assert cancel_subscription(session, active_sub, NOW) is not None
    assert cancel_subscription(session, active_sub, NOW) is None

    assert session.query(Job).count() == 1


def test_status_message_has_tariff_amount_and_next_charge(active_sub):
    message = status_message(active_sub, today=TODAY)

    assert "Статус вашей подписки: Активна до 22.11.2026" in message
    assert "Тариф: 1 месяц" in message
    assert f"Сумма: {active_sub.tariff.price_kopecks // 100} ₽" in message
    assert "Дата следующего списания: 22.11.2026" in message


@respx.mock
def test_card_change_creates_one_ruble_recurring_payment(session, active_sub):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, payment_url = card_change.start(session, active_sub.client)

    body = json.loads(route.calls.last.request.content)
    assert body["Amount"] == 100
    assert body["Recurrent"] == "Y"
    assert body["CustomerKey"] == "920001"
    assert body["NotificationURL"] == NOTIFICATION_URL
    assert body["Receipt"]["Items"][0]["Price"] == 100
    assert body["Receipt"]["Items"][0]["Amount"] == 100
    assert body["Receipt"]["Items"][0]["Tax"] == "none"
    assert '"' not in body["Description"]

    assert payment_url == "https://pay.example/888001"
    assert payment.kind == PaymentKind.card_change
    assert payment.subscription_id == active_sub.id
    assert payment.amount_kopecks == 100
    assert payment.status == PaymentStatus.pending
    assert payment.bank_payment_id == "888001"


def test_card_change_requires_open_subscription(session):
    client = Client(tg_id=920002, phone="+79990000000")
    session.add(client)
    session.flush()

    with pytest.raises(PaymentError):
        card_change.start(session, client)

    assert session.query(Payment).count() == 0


@respx.mock
def test_renew_after_cancel_creates_renewal_payment(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _ = create_tariff_payment(session, active_sub.client, "m1", today=TODAY)

    assert payment.kind == PaymentKind.renewal
    assert payment.subscription_id == active_sub.id
    assert payment.tariff_id == active_sub.tariff_id
    assert payment.amount_kopecks == active_sub.tariff.price_kopecks


def test_confirmed_renewal_restores_auto_renew_and_card(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)
    payment = Payment(
        client_id=active_sub.client_id,
        subscription_id=active_sub.id,
        tariff_id=active_sub.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=active_sub.tariff.price_kopecks,
        order_id="order-renew-1",
        bank_payment_id="pay-renew-1",
        status=PaymentStatus.pending,
    )
    session.add(payment)
    session.flush()

    payload = {
        "TerminalKey": TERMINAL_KEY,
        "OrderId": "order-renew-1",
        "Success": True,
        "Status": "CONFIRMED",
        "PaymentId": "pay-renew-1",
        "Amount": active_sub.tariff.price_kopecks,
        "RebillId": "new-rebill",
        "Pan": "430000******1234",
    }
    payload["Token"] = tbank.make_token(payload, PASSWORD)

    handle_notification(
        session,
        payload,
        now=datetime(2026, 11, 22, 12, 0, tzinfo=MOSCOW),
    )

    assert active_sub.status == SubscriptionStatus.active
    assert active_sub.auto_renew is True
    assert active_sub.rebill_id == "new-rebill"
    assert active_sub.card_mask == "430000******1234"
    assert active_sub.current_period_end == date(2026, 12, 22)
    assert active_sub.failed_attempts == 0
    assert payment.status == PaymentStatus.confirmed


def test_status_message_for_cancelled_uses_cancel_text(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)

    message = status_message(active_sub, today=TODAY)

    assert "Подписка отменена!" in message
    assert "Тариф: 1 месяц" in message
    assert "Дата следующего списания" not in message


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_status_view_cancelled_tomorrow_shows_cancel_text(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)
    active_sub.current_period_end = TODAY + timedelta(days=1)

    text, markup = status_view(active_sub, today=TODAY)

    assert "Подписка отменена!" in text
    assert "Доступ в клуб сохраняется до: 24.10.2026" in text
    assert "manage:renew" in _callbacks(markup)
    assert "menu:subscribe" not in _callbacks(markup)


def test_status_view_cancelled_today_shows_ended_text_and_subscribe(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)
    active_sub.current_period_end = TODAY

    text, markup = status_view(active_sub, today=TODAY)

    assert "Анна, к сожалению, подписка окончена" in text
    assert "Подписка отменена!" not in text
    assert "сохраняется до" not in text
    assert "menu:subscribe" in _callbacks(markup)


def test_status_view_cancelled_yesterday_shows_ended_text_and_subscribe(session, active_sub):
    subscriptions.cancel(session, active_sub, NOW)
    active_sub.current_period_end = TODAY - timedelta(days=1)

    text, markup = status_view(active_sub, today=TODAY)

    assert "Анна, к сожалению, подписка окончена" in text
    assert "Подписка отменена!" not in text
    assert "menu:subscribe" in _callbacks(markup)
