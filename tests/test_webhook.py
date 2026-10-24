import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app import content, texts
from app.config import settings
from app.models import (
    Client,
    Job,
    JobType,
    Payment,
    PaymentEvent,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import billing, jobs
from app.services.payments import handle_notification
from app.tbank import make_token
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
PASSWORD = "test-password"
TERMINAL_KEY = "TestTerminal"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)


def _m1_price() -> int:
    return next(t.price_kopecks for t in content.load_tariffs() if t.code == "m1")


@pytest.fixture(autouse=True)
def tbank_settings(monkeypatch):
    monkeypatch.setattr(settings, "tbank_password", PASSWORD)
    monkeypatch.setattr(settings, "tbank_terminal_key", TERMINAL_KEY)


@pytest.fixture(autouse=True)
def clean_webhook_tables(session):
    _truncate(session)
    yield
    session.rollback()
    _truncate(session)


def _truncate(session):
    session.execute(
        text(
            "TRUNCATE payment_events, payments, subscriptions, messages, jobs, "
            "admins, clients, tariffs RESTART IDENTITY CASCADE"
        )
    )
    session.commit()


@pytest.fixture
def client_row(session):
    row = Client(tg_id=7001, first_name="Анна", email="anna@example.com")
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def tariff_m1(session):
    seed_tariffs(session)
    session.commit()
    return session.query(Tariff).filter_by(code="m1").one()


@pytest.fixture
def initial_payment(session, client_row, tariff_m1):
    payment = Payment(
        client_id=client_row.id,
        tariff_id=tariff_m1.id,
        kind=PaymentKind.initial,
        amount_kopecks=tariff_m1.price_kopecks,
        order_id="order-initial-1",
        bank_payment_id="pay-initial-1",
    )
    session.add(payment)
    session.commit()
    return payment


def signed_payload(**overrides):
    payload = {
        "TerminalKey": TERMINAL_KEY,
        "OrderId": "order-initial-1",
        "Success": True,
        "Status": "CONFIRMED",
        "PaymentId": "pay-initial-1",
        "Amount": _m1_price(),
        "RebillId": "rebill-1",
        "Pan": "430000******0777",
    }
    payload.update(overrides)
    payload["Token"] = make_token(payload, PASSWORD)
    return payload


def _reload(session, instance):
    session.expire_all()
    return session.get(type(instance), instance.id)


def test_handle_notification_activates_initial_payment(session, initial_payment, tariff_m1):
    event = handle_notification(session, signed_payload(), now=NOW)
    session.flush()

    sub = session.query(Subscription).one()
    assert event.signature_ok is True
    assert sub.status == SubscriptionStatus.active
    assert sub.current_period_end == date(2026, 11, 22)
    assert sub.rebill_id == "rebill-1"
    assert sub.card_mask == "430000******0777"
    assert initial_payment.status == PaymentStatus.confirmed
    assert initial_payment.confirmed_at == NOW
    assert initial_payment.subscription_id == sub.id
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1
    cycle_types = {
        job.type for job in session.query(Job).filter(Job.type != JobType.post_payment).all()
    }
    assert {JobType.reminder_3d, JobType.reminder_1d, JobType.charge} <= cycle_types


def test_handle_notification_extends_renewal(session, client_row, tariff_m1):
    subscription = Subscription(
        client=client_row,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()
    payment = Payment(
        client_id=client_row.id,
        subscription_id=subscription.id,
        tariff_id=tariff_m1.id,
        kind=PaymentKind.renewal,
        amount_kopecks=tariff_m1.price_kopecks,
        order_id="order-renewal-1",
        bank_payment_id="pay-renewal-1",
    )
    session.add(payment)
    session.flush()
    old_charge = jobs.enqueue(
        session,
        JobType.charge.value,
        NOW,
        {"subscription_id": subscription.id},
    )
    session.flush()

    handle_notification(
        session,
        signed_payload(
            OrderId="order-renewal-1",
            PaymentId="pay-renewal-1",
            RebillId="rebill-1",
            Pan="430000******0777",
        ),
        now=datetime(2026, 11, 22, 12, 0, tzinfo=MOSCOW),
    )
    session.flush()

    assert subscription.current_period_end == date(2026, 12, 22)
    assert subscription.status == SubscriptionStatus.active
    assert payment.status == PaymentStatus.confirmed
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1
    charges = session.query(Job).filter_by(type=JobType.charge).all()
    assert len(charges) == 1
    assert charges[0].id != old_charge.id
    assert charges[0].run_at == datetime(2026, 12, 22, 9, 0, tzinfo=MOSCOW)


def test_handle_notification_card_change_updates_card(session, client_row, tariff_m1):
    subscription = Subscription(
        client=client_row,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=NOW,
        current_period_end=date(2026, 11, 22),
        rebill_id="old-rebill",
        card_mask="430000******0000",
    )
    session.add(subscription)
    session.flush()
    payment = Payment(
        client_id=client_row.id,
        subscription_id=subscription.id,
        tariff_id=tariff_m1.id,
        kind=PaymentKind.card_change,
        amount_kopecks=100,
        order_id="order-card-1",
        bank_payment_id="pay-card-1",
    )
    session.add(payment)
    session.flush()

    handle_notification(
        session,
        signed_payload(
            OrderId="order-card-1",
            PaymentId="pay-card-1",
            Amount=100,
            RebillId="new-rebill",
            Pan="430000******1234",
        ),
        now=NOW,
    )
    session.flush()

    assert subscription.rebill_id == "new-rebill"
    assert subscription.card_mask == "430000******1234"
    assert subscription.current_period_end == date(2026, 11, 22)
    assert payment.status == PaymentStatus.confirmed
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 0


def test_duplicate_notification_is_idempotent(client, session, initial_payment):
    body = signed_payload()

    assert client.post("/tbank/notify", json=body).text == "OK"
    session.expire_all()
    subscription = session.query(Subscription).one()
    assert subscription.current_period_end > date.today()

    subscription.current_period_end = date(2027, 1, 1)
    session.commit()

    assert client.post("/tbank/notify", json=body).text == "OK"
    session.expire_all()
    subscription = session.query(Subscription).one()
    assert subscription.current_period_end == date(2027, 1, 1)
    assert session.query(Subscription).count() == 1
    assert session.query(PaymentEvent).count() == 2
    assert _reload(session, initial_payment).status == PaymentStatus.confirmed
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1
    assert session.query(Job).filter_by(type=JobType.charge).count() == 1


def test_invalid_signature_returns_403_and_records_event(client, session, initial_payment):
    body = signed_payload()
    body["Amount"] = 100  # подпись больше не совпадает

    response = client.post("/tbank/notify", json=body)

    assert response.status_code == 403
    session.expire_all()
    event = session.query(PaymentEvent).one()
    assert event.signature_ok is False
    assert session.query(Subscription).count() == 0
    assert _reload(session, initial_payment).status == PaymentStatus.created


def test_amount_mismatch_keeps_payment_and_logs_alert(client, session, initial_payment, caplog):
    body = signed_payload(Amount=100)  # подпись валидна, сумма не та
    caplog.set_level(logging.ERROR, logger="app.services.payments")

    response = client.post("/tbank/notify", json=body)

    assert response.status_code == 200
    assert response.text == "OK"
    session.expire_all()
    assert session.query(Subscription).count() == 0
    assert _reload(session, initial_payment).status == PaymentStatus.created
    assert session.query(PaymentEvent).count() == 1
    assert any("amount mismatch" in record.message for record in caplog.records)


def test_unknown_payment_id_records_event_and_returns_ok(client, session):
    body = signed_payload(OrderId="missing-order", PaymentId="missing-payment")

    response = client.post("/tbank/notify", json=body)

    assert response.status_code == 200
    assert response.text == "OK"
    session.expire_all()
    assert session.query(PaymentEvent).count() == 1
    assert session.query(Payment).count() == 0


def test_payment_found_by_order_id_when_bank_id_missing(client, session, initial_payment):
    initial_payment.bank_payment_id = None
    session.commit()

    body = signed_payload(PaymentId="pay-new-from-bank")

    assert client.post("/tbank/notify", json=body).text == "OK"
    session.expire_all()
    payment = _reload(session, initial_payment)
    assert payment.bank_payment_id == "pay-new-from-bank"
    assert payment.status == PaymentStatus.confirmed
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1
    assert session.query(Subscription).count() == 1


@pytest.mark.parametrize("status", ["REJECTED", "CANCELED", "CANCELLED"])
def test_rejected_status_marks_payment_and_does_not_activate(
    client, session, initial_payment, status
):
    body = signed_payload(
        Status=status,
        Success=False,
        ErrorCode="7",
        Message="Insufficient funds",
    )

    response = client.post("/tbank/notify", json=body)

    assert response.status_code == 200
    assert response.text == "OK"
    session.expire_all()
    payment = _reload(session, initial_payment)
    assert payment.status == PaymentStatus.rejected
    assert payment.error_code == "7"
    assert payment.error_message == "Insufficient funds"
    assert session.query(Subscription).count() == 0
    assert session.query(PaymentEvent).count() == 1


def test_duplicate_rejected_notification_changes_nothing(client, session, initial_payment):
    first = signed_payload(Status="REJECTED", Success=False, ErrorCode="7", Message="first error")
    second = signed_payload(Status="REJECTED", Success=False, ErrorCode="8", Message="second error")

    assert client.post("/tbank/notify", json=first).text == "OK"
    assert client.post("/tbank/notify", json=second).text == "OK"

    session.expire_all()
    payment = _reload(session, initial_payment)
    assert payment.status == PaymentStatus.rejected
    assert payment.error_code == "7"
    assert payment.error_message == "first error"
    assert session.query(Subscription).count() == 0
    assert session.query(PaymentEvent).count() == 2


def test_other_statuses_do_not_activate(client, session, initial_payment):
    body = signed_payload(Status="AUTHORIZED")

    response = client.post("/tbank/notify", json=body)

    assert response.status_code == 200
    session.expire_all()
    assert session.query(Subscription).count() == 0
    assert _reload(session, initial_payment).status == PaymentStatus.created
    assert session.query(PaymentEvent).count() == 1


def test_rejected_manual_initial_enqueues_notice_once(
    client, session, initial_payment, monkeypatch
):
    notifications: list[tuple[int, str]] = []
    monkeypatch.setattr(
        billing,
        "notify",
        lambda tg_id, text, reply_markup=None: notifications.append((tg_id, text)),
    )
    body = signed_payload(
        Status="REJECTED",
        Success=False,
        ErrorCode="7",
        Message="Insufficient funds",
    )

    assert client.post("/tbank/notify", json=body).text == "OK"
    assert client.post("/tbank/notify", json=body).text == "OK"

    session.expire_all()
    notices = session.query(Job).filter_by(type=JobType.payment_failed).all()
    assert len(notices) == 1
    assert notices[0].payload == {"payment_id": initial_payment.id}
    assert notices[0].status.value == "pending"

    billing.handle_payment_failed(session, notices[0])

    assert notifications == [(7001, texts.PAYMENT_FAILED)]


def test_rejected_manual_renewal_enqueues_notice(session, client_row, tariff_m1, monkeypatch):
    subscription = Subscription(
        client=client_row,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()
    payment = Payment(
        client_id=client_row.id,
        subscription_id=subscription.id,
        tariff_id=tariff_m1.id,
        kind=PaymentKind.renewal,
        amount_kopecks=tariff_m1.price_kopecks,
        order_id="order-renewal-rejected",
        bank_payment_id="pay-renewal-rejected",
    )
    session.add(payment)
    session.flush()

    handle_notification(
        session,
        signed_payload(
            OrderId="order-renewal-rejected",
            PaymentId="pay-renewal-rejected",
            Status="REJECTED",
            Success=False,
            ErrorCode="1051",
            Message="Insufficient funds",
        ),
        now=NOW,
    )
    session.flush()

    notices = session.query(Job).filter_by(type=JobType.payment_failed).all()
    assert len(notices) == 1
    assert notices[0].payload == {"payment_id": payment.id}


def test_worker_rejection_does_not_enqueue_manual_notice(session, client_row, tariff_m1):
    subscription = Subscription(
        client=client_row,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()
    payment = Payment(
        client_id=client_row.id,
        subscription_id=subscription.id,
        tariff_id=tariff_m1.id,
        kind=PaymentKind.renewal,
        amount_kopecks=tariff_m1.price_kopecks,
        order_id="order-worker-rejected",
        bank_payment_id="pay-worker-rejected",
        initiated_by_worker=True,
    )
    session.add(payment)
    session.flush()

    handle_notification(
        session,
        signed_payload(
            OrderId="order-worker-rejected",
            PaymentId="pay-worker-rejected",
            Status="REJECTED",
            Success=False,
            ErrorCode="1051",
            Message="Insufficient funds",
        ),
        now=NOW,
    )
    session.flush()

    assert session.query(Job).filter_by(type=JobType.payment_failed).count() == 0
