from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.models import Client, Payment, PaymentKind, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs
from app.services.subscriptions import (
    activate,
    cancel,
    extend,
    has_access,
    set_billing_enabled,
)

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture
def client(session):
    # Локально перекрывает HTTP-фикстуру client из conftest: здесь нужен Client из БД.
    row = Client(tg_id=5001, first_name="Анна", email="anna@example.com")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def tariff_m1(session):
    seed_tariffs(session)
    return session.query(Tariff).filter_by(code="m1").one()


@pytest.fixture
def tariff_m1_legacy(session):
    seed_tariffs(session)
    return session.query(Tariff).filter_by(code="m1_legacy").one()


def _payment(session, client, order_id, amount_kopecks):
    payment = Payment(
        client_id=client.id,
        kind=PaymentKind.initial,
        amount_kopecks=amount_kopecks,
        order_id=order_id,
    )
    session.add(payment)
    session.flush()
    return payment


def test_activation_uses_moscow_date(session, client, tariff_m1):
    now = datetime(2026, 10, 23, 23, 30, tzinfo=MOSCOW)
    payment = _payment(session, client, "order-activate-1", tariff_m1.price_kopecks)

    sub = activate(session, client, tariff_m1, payment, now)

    assert sub.current_period_end == date(2026, 11, 22)
    assert sub.status == SubscriptionStatus.active
    assert sub.auto_renew is True
    assert sub.billing_enabled is True
    assert sub.failed_attempts == 0
    assert sub.started_at == now
    assert payment.subscription_id == sub.id


def test_activation_converts_utc_to_moscow_date(session, client, tariff_m1):
    # 20:30 UTC = 23:30 МСК; период должен считаться от московской даты.
    now = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)
    payment = _payment(session, client, "order-activate-2", tariff_m1.price_kopecks)

    sub = activate(session, client, tariff_m1, payment, now)

    assert sub.current_period_end == date(2026, 11, 22)


def test_extend_keeps_legacy_tariff_and_price(session, client, tariff_m1_legacy):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
    first_payment = _payment(session, client, "order-legacy-1", tariff_m1_legacy.price_kopecks)
    sub = activate(session, client, tariff_m1_legacy, first_payment, now)
    sub.current_period_end = date(2026, 10, 23)  # продление в день окончания
    sub.status = SubscriptionStatus.past_due
    sub.failed_attempts = 2

    renewal = _payment(session, client, "order-legacy-2", tariff_m1_legacy.price_kopecks)
    extend(session, sub, renewal, now)

    assert sub.tariff_id == tariff_m1_legacy.id
    assert sub.tariff.code == "m1_legacy"
    assert renewal.amount_kopecks == tariff_m1_legacy.price_kopecks
    assert sub.current_period_end == date(2026, 11, 22)
    assert sub.status == SubscriptionStatus.active
    assert sub.failed_attempts == 0
    assert renewal.subscription_id == sub.id


def test_extend_preserves_remaining_period(session, client, tariff_m1):
    now = datetime(2026, 10, 24, 12, 0, tzinfo=MOSCOW)
    first_payment = _payment(session, client, "order-early-1", tariff_m1.price_kopecks)
    sub = activate(session, client, tariff_m1, first_payment, now)
    sub.current_period_end = date(2026, 11, 22)

    renewal = _payment(session, client, "order-early-2", tariff_m1.price_kopecks)
    extend(session, sub, renewal, now)

    assert sub.current_period_end == date(2026, 12, 22)


def test_extend_from_charge_date_when_period_already_over(session, client, tariff_m1):
    now = datetime(2026, 10, 24, 12, 0, tzinfo=MOSCOW)
    first_payment = _payment(session, client, "order-overdue-1", tariff_m1.price_kopecks)
    sub = activate(session, client, tariff_m1, first_payment, now)
    sub.current_period_end = date(2026, 10, 1)

    renewal = _payment(session, client, "order-overdue-2", tariff_m1.price_kopecks)
    extend(session, sub, renewal, now)

    # Месячный период считается от текущего окончания и всегда падает на 22-е.
    assert sub.current_period_end == date(2026, 11, 22)


def test_cancel_disables_renewal_and_keeps_access_until_period_end(session, client, tariff_m1):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
    payment = _payment(session, client, "order-cancel-1", tariff_m1.price_kopecks)
    sub = activate(session, client, tariff_m1, payment, now)
    sub.rebill_id = "rebill-1"
    period_end = sub.current_period_end

    cancel(session, sub, now)

    assert sub.auto_renew is False
    assert sub.rebill_id is None
    assert sub.current_period_end == period_end
    assert sub.status == SubscriptionStatus.cancelled


def test_set_billing_enabled_persists_flag(session, client, tariff_m1):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
    payment = _payment(session, client, "order-billing-1", tariff_m1.price_kopecks)
    sub = activate(session, client, tariff_m1, payment, now)

    set_billing_enabled(session, sub, False)
    assert sub.billing_enabled is False

    set_billing_enabled(session, sub, True)
    assert sub.billing_enabled is True


TODAY = date(2026, 10, 23)


@pytest.mark.parametrize(
    ("status", "period_end", "expected"),
    [
        (SubscriptionStatus.active, date(2026, 9, 1), True),
        (SubscriptionStatus.past_due, date(2026, 9, 1), True),
        (SubscriptionStatus.cancelled, date(2026, 11, 22), True),
        (SubscriptionStatus.cancelled, date(2026, 10, 22), False),
        (SubscriptionStatus.expired, date(2026, 12, 1), False),
        (SubscriptionStatus.expired, date(2026, 9, 1), False),
    ],
)
def test_has_access_by_status(session, client, tariff_m1, status, period_end, expected):
    subscription = Subscription(
        client=client,
        tariff=tariff_m1,
        status=status,
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=period_end,
    )
    session.add(subscription)
    session.flush()

    assert has_access(subscription, TODAY) is expected


def test_has_access_without_subscription():
    assert has_access(None, TODAY) is False


def test_extend_uses_forced_period_end_and_clears_it(session, client, tariff_m1):
    now = datetime(2026, 10, 24, 12, 0, tzinfo=MOSCOW)
    first_payment = _payment(session, client, "order-forced-1", tariff_m1.price_kopecks)
    sub = activate(session, client, tariff_m1, first_payment, now)
    sub.forced_period_end = date(2026, 11, 22)
    renewal = _payment(session, client, "order-forced-2", tariff_m1.price_kopecks)

    extend(session, sub, renewal, now)

    # forced_period_end — абсолютная дата окончания (якорь 22-го), не база для months.
    assert sub.current_period_end == date(2026, 11, 22)
    assert sub.forced_period_end is None
