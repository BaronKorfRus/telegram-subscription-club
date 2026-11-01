from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.models import Client, Payment, PaymentKind, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs
from app.services import subscriptions

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture
def client(session):
    row = Client(tg_id=6001, first_name="Анна", email="anna@example.com")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def tariff_m1(session):
    seed_tariffs(session)
    return session.query(Tariff).filter_by(code="m1").one()


def _payment(session, client, order_id):
    payment = Payment(
        client_id=client.id,
        kind=PaymentKind.initial,
        amount_kopecks=100,
        order_id=order_id,
    )
    session.add(payment)
    session.flush()
    return payment


def test_period_end_same_month_anchor():
    assert subscriptions._period_end(datetime(2026, 10, 24, 12, tzinfo=MOSCOW), 1) == date(
        2026, 11, 22
    )


def test_period_end_three_and_six_months_cross_year():
    assert subscriptions._period_end(datetime(2026, 10, 24, 12, tzinfo=MOSCOW), 3) == date(
        2027, 1, 22
    )
    assert subscriptions._period_end(datetime(2026, 10, 24, 12, tzinfo=MOSCOW), 6) == date(
        2027, 4, 22
    )


def test_period_end_from_22nd_stays_on_22nd():
    assert subscriptions._period_end(datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW), 1) == date(
        2026, 12, 22
    )


def test_period_end_crosses_december_and_february():
    assert subscriptions._period_end(datetime(2026, 12, 24, 9, 0, tzinfo=MOSCOW), 1) == date(
        2027, 1, 22
    )
    assert subscriptions._period_end(datetime(2027, 1, 23, 9, 0, tzinfo=MOSCOW), 1) == date(
        2027, 2, 22
    )


def test_add_months_keeps_the_22nd_anchor():
    assert subscriptions.add_months(date(2026, 11, 22), 1) == date(2026, 12, 22)
    assert subscriptions.add_months(date(2026, 11, 1), 1) == date(2026, 12, 22)
    assert subscriptions.add_months(date(2027, 2, 13), 1) == date(2027, 3, 22)


def test_activate_uses_period_months(session, client, tariff_m1):
    payment = _payment(session, client, "order-months-activate")
    sub = subscriptions.activate(
        session, client, tariff_m1, payment, datetime(2026, 10, 24, 12, tzinfo=MOSCOW)
    )
    assert tariff_m1.period_months == 1
    assert sub.current_period_end == date(2026, 11, 22)


def test_extend_from_existing_end_not_from_payment_day(session, client, tariff_m1):
    # Подписка до 22.11, оплата 24.11 на месяц → 22.12, а не 24.12.
    sub = Subscription(
        client=client,
        tariff=tariff_m1,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 24, 12, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(sub)
    session.flush()
    payment = _payment(session, client, "order-months-extend")

    subscriptions.extend(session, sub, payment, datetime(2026, 11, 24, 12, tzinfo=MOSCOW))

    assert sub.current_period_end == date(2026, 12, 22)


def test_extend_respects_forced_period_end(session, client, tariff_m1):
    sub = Subscription(
        client=client,
        tariff=tariff_m1,
        status=SubscriptionStatus.past_due,
        started_at=datetime(2026, 10, 1, 12, tzinfo=MOSCOW),
        current_period_end=date(2026, 10, 30),
        forced_period_end=date(2026, 11, 22),
    )
    session.add(sub)
    session.flush()
    payment = _payment(session, client, "order-months-forced")

    subscriptions.extend(session, sub, payment, datetime(2026, 11, 23, 12, tzinfo=MOSCOW))

    assert sub.current_period_end == date(2026, 11, 22)
    assert sub.forced_period_end is None
