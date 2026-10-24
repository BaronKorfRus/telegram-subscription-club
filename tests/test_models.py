from datetime import UTC, date, datetime

import pytest
from app import content
from app.db import engine
from app.models import Client, Payment, PaymentKind, Subscription, Tariff
from app.seed import seed_tariffs
from sqlalchemy import Date, inspect
from sqlalchemy.exc import IntegrityError


def test_tariffs_seeded_and_legacy_flag(session):
    seed_tariffs(session)
    rows = {t.code: t for t in session.query(Tariff).all()}
    expected = {t.code: t for t in content.load_tariffs()}
    assert rows["m1"].price_kopecks == expected["m1"].price_kopecks and not rows["m1"].is_legacy
    assert (
        rows["m1_legacy"].price_kopecks == expected["m1_legacy"].price_kopecks
        and rows["m1_legacy"].is_legacy
    )
    assert len(rows) == 6


def test_tariffs_full_table(session):
    seed_tariffs(session)
    rows = {t.code: t for t in session.query(Tariff).all()}
    expected = {
        t.code: (t.price_kopecks, t.period_days, t.is_active)
        for t in content.load_tariffs()
    }
    assert {code: (t.price_kopecks, t.period_days, t.is_active) for code, t in rows.items()} == (
        expected
    )


def test_seed_is_idempotent(session):
    seed_tariffs(session)
    seed_tariffs(session)
    assert session.query(Tariff).count() == 6


def test_current_period_end_is_calendar_date(session):
    assert isinstance(Subscription.__table__.c.current_period_end.type, Date)

    db_columns = {c["name"]: c for c in inspect(engine).get_columns("subscriptions")}
    assert isinstance(db_columns["current_period_end"]["type"], Date)

    seed_tariffs(session)
    client = Client(tg_id=4001)
    session.add(client)
    session.flush()
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client_id=client.id,
        tariff_id=tariff.id,
        started_at=datetime(2026, 10, 23, 23, 30, tzinfo=UTC),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()
    session.refresh(subscription)
    assert subscription.current_period_end == date(2026, 11, 22)


def test_client_tg_id_is_unique(session):
    session.add(Client(tg_id=1001))
    session.flush()
    session.add(Client(tg_id=1001))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def _payment(client, order_id, bank_payment_id=None):
    return Payment(
        client_id=client.id,
        kind=PaymentKind.initial,
        amount_kopecks=99000,
        order_id=order_id,
        bank_payment_id=bank_payment_id,
    )


def test_bank_payment_id_is_nullable_but_unique(session):
    client = Client(tg_id=2001)
    session.add(client)
    session.flush()

    # bank_payment_id nullable: два платежа без него уживаются.
    session.add_all([_payment(client, "order-1"), _payment(client, "order-2")])
    session.flush()

    session.add(_payment(client, "order-3", bank_payment_id="bank-1"))
    session.flush()

    session.add(_payment(client, "order-4", bank_payment_id="bank-1"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_payment_order_id_is_unique(session):
    client = Client(tg_id=3001)
    session.add(client)
    session.flush()

    session.add_all([_payment(client, "order-1"), _payment(client, "order-1")])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
