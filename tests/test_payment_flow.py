import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from app import receipts, tbank
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Client,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services.payments import (
    PaymentError,
    create_payment,
    create_tariff_payment,
    offered_tariffs,
    open_subscription,
    renewal_price,
)
from sqlalchemy import select, text

MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 23)
TERMINAL_KEY = "TestTerminal"
PASSWORD = "test-password"
NOTIFICATION_URL = "https://callback.example/tbank/notify"


@pytest.fixture(autouse=True)
def tbank_settings(monkeypatch):
    monkeypatch.setattr(settings, "tbank_terminal_key", TERMINAL_KEY)
    monkeypatch.setattr(settings, "tbank_password", PASSWORD)
    monkeypatch.setattr(settings, "tbank_notification_url", NOTIFICATION_URL)


@pytest.fixture(autouse=True)
def clean_payment_tables(session):
    _truncate(session)
    yield
    session.rollback()
    _truncate(session)


def _truncate(session):
    session.execute(
        text("TRUNCATE payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()


@pytest.fixture
def tariff_m1(session):
    seed_tariffs(session)
    return session.query(Tariff).filter_by(code="m1").one()


@pytest.fixture
def new_client(session):
    client = Client(
        tg_id=910001,
        first_name="Анна",
        email="anna@example.com",
        phone="+79991234567",
    )
    session.add(client)
    session.flush()
    return client


@pytest.fixture
def legacy_subscription(session):
    seed_tariffs(session)
    client = Client(
        tg_id=910002,
        first_name="Мария",
        email="maria@example.com",
        phone="+79997654321",
    )
    session.add(client)
    session.flush()
    tariff = session.query(Tariff).filter_by(code="m1_legacy").one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 1),
    )
    session.add(subscription)
    session.flush()
    return subscription


def _init_ok():
    return httpx.Response(
        200,
        json={
            "Success": True,
            "PaymentId": "777001",
            "PaymentURL": "https://pay.example/777001",
        },
    )


def test_new_client_sees_only_new_tariffs(session, new_client):
    seed_tariffs(session)

    codes = [tariff.code for tariff in offered_tariffs(session, new_client, today=TODAY)]

    assert codes == ["m1", "m3", "m6"]


def test_legacy_client_keeps_legacy_price(session, legacy_subscription):
    assert renewal_price(session, legacy_subscription) == legacy_subscription.tariff.price_kopecks
    assert [t.code for t in offered_tariffs(session, legacy_subscription.client, today=TODAY)] == [
        "m1_legacy"
    ]


@respx.mock
def test_create_payment_sends_correct_init(session, new_client, tariff_m1):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, payment_url = create_payment(session, new_client, tariff_m1, PaymentKind.initial)

    body = json.loads(route.calls.last.request.content)
    assert body["TerminalKey"] == TERMINAL_KEY
    assert body["Amount"] == tariff_m1.price_kopecks
    assert body["Recurrent"] == "Y"
    assert body["CustomerKey"] == "910001"
    assert body["Receipt"] == receipts.build(tariff_m1, new_client.email, new_client.phone)
    assert body["NotificationURL"] == NOTIFICATION_URL
    assert body["Description"] and '"' not in body["Description"]
    assert body["OrderId"] == payment.order_id
    assert body["Token"] == tbank.make_token(body, PASSWORD)

    assert payment_url == "https://pay.example/777001"
    assert payment.bank_payment_id == "777001"
    assert payment.tariff_id == tariff_m1.id
    assert payment.kind == PaymentKind.initial
    assert payment.status == PaymentStatus.pending
    assert payment.amount_kopecks == tariff_m1.price_kopecks
    assert payment.receipt_json == body["Receipt"]
    assert payment.order_id.startswith("910001-")
    assert len(payment.order_id.split("-", 1)[1]) == 10


@respx.mock
def test_init_runs_after_payment_row_is_committed(session, new_client, tariff_m1):
    observed = {}

    def inspect_then_respond(request):
        body = json.loads(request.content)
        with SessionLocal() as other:
            row = other.scalar(select(Payment).where(Payment.order_id == body["OrderId"]))
            observed["exists"] = row is not None
            observed["status"] = row.status if row else None
            observed["bank_payment_id"] = row.bank_payment_id if row else None
        return _init_ok()

    respx.post(f"{tbank.BASE_URL}/Init").mock(side_effect=inspect_then_respond)

    payment, _ = create_payment(session, new_client, tariff_m1, PaymentKind.initial)

    assert observed == {
        "exists": True,
        "status": PaymentStatus.created,
        "bank_payment_id": None,
    }
    assert payment.status == PaymentStatus.pending
    assert payment.bank_payment_id == "777001"


@respx.mock
def test_legacy_renewal_payment_uses_old_price(session, legacy_subscription):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _ = create_payment(
        session,
        legacy_subscription.client,
        legacy_subscription.tariff,
        PaymentKind.renewal,
        subscription=legacy_subscription,
    )

    body = json.loads(route.calls.last.request.content)
    assert body["Amount"] == legacy_subscription.tariff.price_kopecks
    assert payment.amount_kopecks == legacy_subscription.tariff.price_kopecks
    assert payment.kind == PaymentKind.renewal
    assert payment.tariff_id == legacy_subscription.tariff_id
    assert payment.subscription_id == legacy_subscription.id


@respx.mock
def test_open_subscription_creates_renewal_for_its_tariff(session, legacy_subscription):
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _ = create_tariff_payment(
        session, legacy_subscription.client, "m1_legacy", today=TODAY
    )

    assert payment.kind == PaymentKind.renewal
    assert payment.subscription_id == legacy_subscription.id
    assert payment.tariff_id == legacy_subscription.tariff_id
    assert payment.amount_kopecks == legacy_subscription.tariff.price_kopecks


@respx.mock
def test_open_new_subscription_also_creates_renewal(session, new_client):
    seed_tariffs(session)
    tariff_m3 = session.query(Tariff).filter_by(code="m3").one()
    subscription = Subscription(
        client=new_client,
        tariff=tariff_m3,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 12, 1),
    )
    session.add(subscription)
    session.flush()
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=_init_ok())

    payment, _ = create_tariff_payment(session, new_client, "m3", today=TODAY)

    assert payment.kind == PaymentKind.renewal
    assert payment.subscription_id == subscription.id
    assert payment.tariff_id == tariff_m3.id
    assert payment.amount_kopecks == tariff_m3.price_kopecks


def test_open_subscription_rejects_other_tariff(session, legacy_subscription):
    with pytest.raises(PaymentError):
        create_tariff_payment(session, legacy_subscription.client, "m3", today=TODAY)

    assert session.query(Payment).count() == 0


@respx.mock
def test_init_error_keeps_rejected_payment(session, new_client, tariff_m1):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(
        return_value=httpx.Response(
            200,
            json={"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"},
        )
    )

    with pytest.raises(PaymentError):
        create_payment(session, new_client, tariff_m1, PaymentKind.initial)

    body = json.loads(route.calls.last.request.content)
    session.expire_all()
    row = session.scalar(select(Payment).where(Payment.order_id == body["OrderId"]))
    assert row is not None
    assert row.status == PaymentStatus.rejected
    assert row.error_code == "7"
    assert row.error_message == "Insufficient funds"
    assert row.bank_payment_id is None


@respx.mock
def test_init_without_payment_url_keeps_rejected_payment(session, new_client, tariff_m1):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(
        return_value=httpx.Response(200, json={"Success": True, "PaymentId": "777001"})
    )

    with pytest.raises(PaymentError):
        create_payment(session, new_client, tariff_m1, PaymentKind.initial)

    body = json.loads(route.calls.last.request.content)
    session.expire_all()
    row = session.scalar(select(Payment).where(Payment.order_id == body["OrderId"]))
    assert row is not None
    assert row.status == PaymentStatus.rejected
    assert row.error_code == "invalid_response"
    assert row.bank_payment_id is None


@respx.mock
def test_network_error_keeps_rejected_payment(session, new_client, tariff_m1):
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(PaymentError):
        create_payment(session, new_client, tariff_m1, PaymentKind.initial)

    body = json.loads(route.calls.last.request.content)
    session.expire_all()
    row = session.scalar(select(Payment).where(Payment.order_id == body["OrderId"]))
    assert row is not None
    assert row.status == PaymentStatus.rejected
    assert row.error_code == "network"
    assert row.bank_payment_id is None


@pytest.mark.parametrize(
    ("status", "period_end", "expected"),
    [
        (SubscriptionStatus.past_due, date(2026, 9, 1), True),
        (SubscriptionStatus.cancelled, date(2026, 9, 1), False),
        (SubscriptionStatus.expired, date(2026, 12, 1), False),
    ],
)
def test_open_subscription_uses_shared_access_rule(
    session, new_client, status, period_end, expected
):
    seed_tariffs(session)
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client=new_client,
        tariff=tariff,
        status=status,
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=period_end,
    )
    session.add(subscription)
    session.flush()

    found = open_subscription(session, new_client, today=TODAY)

    assert (found is subscription) is expected
