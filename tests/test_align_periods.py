from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import jobs
from scripts import align_periods
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 26)


@pytest.fixture(autouse=True)
def clean_tables(session):
    session.execute(
        text("TRUNCATE jobs, payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()
    yield
    session.rollback()


def _subscription(session, tg_id, period_end, status=SubscriptionStatus.active, code="m1"):
    seed_tariffs(session)
    client = Client(tg_id=tg_id, first_name="Анна", email="a@example.com")
    session.add(client)
    session.flush()
    tariff = session.query(Tariff).filter_by(code=code).one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=status,
        started_at=datetime(2026, 1, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=period_end,
        auto_renew=True,
        billing_enabled=True,
        rebill_id="rebill-1",
    )
    session.add(subscription)
    session.flush()
    return subscription


def test_plan_aligns_to_same_month_22nd(session):
    first = _subscription(session, 980001, date(2026, 11, 1))
    third = _subscription(session, 980002, date(2027, 2, 13), code="m3")
    irina = _subscription(session, 980003, date(2026, 11, 23))

    rows = {row.subscription_id: row for row in align_periods.plan(session, TODAY)}

    assert rows[first.id].new_end == date(2026, 11, 22)
    assert rows[third.id].new_end == date(2027, 2, 22)
    assert rows[irina.id].new_end == date(2026, 11, 22)


def test_plan_moves_passed_22nd_to_next_month(session):
    late = _subscription(session, 980004, date(2026, 10, 30))

    rows = {row.subscription_id: row for row in align_periods.plan(session, TODAY)}

    assert rows[late.id].old_end == date(2026, 10, 30)
    assert rows[late.id].new_end == date(2026, 11, 22)


def test_plan_skips_cancelled_and_expired(session):
    _subscription(session, 980005, date(2026, 11, 1), status=SubscriptionStatus.cancelled)
    _subscription(session, 980006, date(2026, 11, 1), status=SubscriptionStatus.expired)
    active = _subscription(session, 980007, date(2026, 11, 1))

    rows = align_periods.plan(session, TODAY)

    assert [row.subscription_id for row in rows] == [active.id]


def test_plan_skips_already_aligned(session):
    _subscription(session, 980008, date(2026, 11, 22))

    assert align_periods.plan(session, TODAY) == []


def test_apply_replaces_stale_jobs(session):
    subscription = _subscription(session, 980009, date(2026, 11, 1))
    stale = jobs.enqueue(
        session,
        JobType.charge.value,
        datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW),
        {"subscription_id": subscription.id},
    )
    rows = align_periods.plan(session, TODAY)

    align_periods.apply(session, rows, TODAY)

    session.expire_all()
    assert subscription.current_period_end == date(2026, 11, 22)
    # Устаревшая pending-задача удалена, вместо неё новый цикл.
    assert session.get(Job, stale.id) is None
    charge = session.query(Job).filter_by(type=JobType.charge, status=JobStatus.pending).one()
    assert charge.run_at == datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)
    assert charge.payload["subscription_id"] == subscription.id


def test_apply_is_idempotent(session):
    subscription = _subscription(session, 980010, date(2026, 11, 1))
    rows = align_periods.plan(session, TODAY)
    align_periods.apply(session, rows, TODAY)

    second_plan = align_periods.plan(session, TODAY)
    align_periods.apply(session, second_plan, TODAY)

    charges = session.query(Job).filter_by(type=JobType.charge).all()
    assert len(charges) == 1
    assert subscription.current_period_end == date(2026, 11, 22)


def test_apply_returns_number_of_changed_rows(session):
    _subscription(session, 980011, date(2026, 11, 1))
    _subscription(session, 980012, date(2026, 11, 5))
    rows = align_periods.plan(session, TODAY)

    assert align_periods.apply(session, rows, TODAY) == 2


def test_dry_run_is_default(session, monkeypatch, capsys):
    subscription = _subscription(session, 980013, date(2026, 11, 1))
    session.commit()

    assert align_periods.main(["--date", "2026-10-26"]) == 0
    output = capsys.readouterr().out

    session.expire_all()
    assert subscription.current_period_end == date(2026, 11, 1)
    assert "Dry-run" in output


def test_apply_flag_writes(session, capsys):
    subscription = _subscription(session, 980014, date(2026, 11, 1))
    session.commit()

    assert align_periods.main(["--apply", "--date", "2026-10-26"]) == 0
    capsys.readouterr()

    session.expire_all()
    assert subscription.current_period_end == date(2026, 11, 22)
