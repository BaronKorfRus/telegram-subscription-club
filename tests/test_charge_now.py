from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.charge_now import ChargeNowError, enqueue_charge
from app.models import Client, Job, JobStatus, JobType, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)


@pytest.fixture
def subscription(session):
    seed_tariffs(session)
    client = Client(tg_id=970001, first_name="Тест")
    tariff = session.query(Tariff).filter_by(code="m1").one()
    sub = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=NOW,
        current_period_end=date(2026, 11, 22),
        auto_renew=True,
        billing_enabled=True,
        rebill_id="rebill-test",
    )
    session.add(sub)
    session.flush()
    return sub


def test_enqueue_charge_creates_job_only(session, subscription, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("charge_now не должен вызывать банк")

    monkeypatch.setattr("app.tbank.init_payment", fail)
    monkeypatch.setattr("app.tbank.charge", fail)

    job = enqueue_charge(session, subscription.id, now=NOW)

    assert job.type == JobType.charge
    assert job.status == JobStatus.pending
    assert job.run_at == NOW
    assert job.payload == {"subscription_id": subscription.id}
    assert session.query(Job).filter_by(type=JobType.charge).count() == 1


def test_enqueue_charge_refuses_disabled_subscription(session, subscription):
    subscription.billing_enabled = False
    session.flush()

    with pytest.raises(ChargeNowError):
        enqueue_charge(session, subscription.id, now=NOW)
