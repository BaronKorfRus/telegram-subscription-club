from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from app import billing_worker
from app.models import (
    Client,
    Job,
    JobType,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import billing, jobs, reconcile
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
TODAY = NOW.date()
TG_ID = 960001


@pytest.fixture(autouse=True)
def clean_reconcile_tables(session):
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
def subscription(session):
    seed_tariffs(session)
    client = Client(
        tg_id=TG_ID,
        first_name="Анна",
        email="anna@example.com",
        phone="+79991234567",
    )
    tariff = session.query(Tariff).filter_by(code="m1").one()
    sub = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 9, 23, 12, 0, tzinfo=MOSCOW),
        current_period_end=TODAY,
        auto_renew=True,
        billing_enabled=True,
        rebill_id="rebill-1",
    )
    session.add(sub)
    session.flush()
    return sub


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        billing, "notify", lambda tg_id, text, reply_markup=None: sent.append((tg_id, text))
    )
    return sent


def _pending_renewal(session, subscription) -> Payment:
    payment = Payment(
        client_id=subscription.client_id,
        subscription_id=subscription.id,
        tariff_id=subscription.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=subscription.tariff.price_kopecks,
        order_id="order-reconcile-1",
        bank_payment_id="pay-reconcile-1",
        status=PaymentStatus.pending,
        created_at=NOW - timedelta(minutes=20),
    )
    session.add(payment)
    session.flush()
    return payment


def test_expire_sets_status_and_schedules_kick(session, subscription, notifications):
    subscription.status = SubscriptionStatus.past_due

    job = billing.expire(session, subscription, NOW)

    assert subscription.status == SubscriptionStatus.expired
    assert subscription.auto_renew is False
    assert job is not None and job.type == JobType.kick
    assert job.payload == {"subscription_id": subscription.id, "tg_id": TG_ID}
    assert notifications and "подписка окончена" in notifications[-1][1]


def test_kick_revokes_access(session, subscription, notifications, monkeypatch):
    monkeypatch.setattr(billing, "_now", lambda: NOW)
    subscription.status = SubscriptionStatus.expired
    subscription.auto_renew = False
    subscription.current_period_end = TODAY - timedelta(days=1)
    revoked = []
    monkeypatch.setattr(billing.access, "revoke", lambda tg_id: revoked.append(tg_id))
    job = SimpleNamespace(payload={"subscription_id": subscription.id, "tg_id": TG_ID})

    billing.handle_kick(session, job)

    assert revoked == [TG_ID]
    assert notifications


@pytest.mark.parametrize(
    ("period_end", "auto_renew"),
    [
        (TODAY + timedelta(days=10), False),
        (TODAY - timedelta(days=1), True),
    ],
)
def test_kick_skips_renewed_or_future_subscription(
    session, subscription, notifications, monkeypatch, period_end, auto_renew
):
    monkeypatch.setattr(billing, "_now", lambda: NOW)
    subscription.current_period_end = period_end
    subscription.auto_renew = auto_renew
    revoked = []
    monkeypatch.setattr(billing.access, "revoke", lambda tg_id: revoked.append(tg_id))
    job = SimpleNamespace(payload={"subscription_id": subscription.id, "tg_id": TG_ID})

    billing.handle_kick(session, job)

    assert revoked == []
    assert notifications == []


def test_reconcile_confirms_pending_once(session, subscription, monkeypatch, notifications):
    payment = _pending_renewal(session, subscription)
    calls = []

    def get_state(payment_id):
        calls.append(payment_id)
        return {"Success": True, "Status": "CONFIRMED"}

    monkeypatch.setattr(reconcile.tbank, "get_state", get_state)

    assert reconcile.run(session, NOW) == 1
    assert payment.status == PaymentStatus.confirmed
    assert subscription.current_period_end == date(2026, 11, 22)
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1

    assert reconcile.run(session, NOW) == 0
    assert len(calls) == 1
    assert subscription.current_period_end == date(2026, 11, 22)
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1


def test_reconcile_rejected_goes_through_failure_flow(
    session, subscription, monkeypatch, notifications
):
    payment = _pending_renewal(session, subscription)
    monkeypatch.setattr(
        reconcile.tbank,
        "get_state",
        lambda payment_id: {
            "Success": False,
            "Status": "REJECTED",
            "ErrorCode": "7",
            "Message": "Insufficient funds",
        },
    )

    assert reconcile.run(session, NOW) == 1

    assert payment.status == PaymentStatus.rejected
    assert payment.error_code == "7"
    assert subscription.status == SubscriptionStatus.past_due
    assert subscription.failed_attempts == 1
    assert session.query(Job).filter_by(type=JobType.charge).count() == 1


def test_reconcile_confirms_initial_and_activates(session, subscription, monkeypatch):
    payment = Payment(
        client_id=subscription.client_id,
        tariff_id=subscription.tariff_id,
        kind=PaymentKind.initial,
        amount_kopecks=subscription.tariff.price_kopecks,
        order_id="order-reconcile-initial",
        bank_payment_id="pay-reconcile-initial",
        status=PaymentStatus.pending,
        created_at=NOW - timedelta(minutes=20),
    )
    session.add(payment)
    session.flush()
    monkeypatch.setattr(
        reconcile.tbank,
        "get_state",
        lambda payment_id: {"Success": True, "Status": "CONFIRMED"},
    )

    assert reconcile.run(session, NOW) == 1

    assert payment.status == PaymentStatus.confirmed
    assert payment.subscription_id is not None
    activated = session.get(Subscription, payment.subscription_id)
    assert activated.status == SubscriptionStatus.active
    assert activated.current_period_end == date(2026, 11, 22)
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1


def test_kick_retries_on_access_error(session, subscription, notifications, monkeypatch):
    monkeypatch.setattr(billing, "_now", lambda: NOW)
    subscription.status = SubscriptionStatus.expired
    subscription.auto_renew = False
    subscription.current_period_end = TODAY - timedelta(days=1)
    attempts = []

    def revoke(tg_id):
        attempts.append(tg_id)
        if len(attempts) == 1:
            raise billing.access.AccessError("telegram down")

    monkeypatch.setattr(billing.access, "revoke", revoke)
    job = SimpleNamespace(id=101, payload={"subscription_id": subscription.id, "tg_id": TG_ID})

    billing.handle_kick(session, job)

    assert attempts == [TG_ID]
    assert notifications == []
    retry = session.query(Job).filter(Job.type == JobType.kick, Job.id != 101).one()
    assert retry.run_at == NOW + timedelta(minutes=5)

    billing.handle_kick(session, retry)

    assert attempts == [TG_ID, TG_ID]
    assert notifications
    assert session.query(Job).filter(Job.type == JobType.kick).count() == 1


def test_kick_gives_up_after_24h(session, subscription, monkeypatch):
    monkeypatch.setattr(billing, "_now", lambda: NOW)
    subscription.status = SubscriptionStatus.expired
    subscription.auto_renew = False
    subscription.current_period_end = TODAY - timedelta(days=1)
    monkeypatch.setattr(
        billing.access,
        "revoke",
        lambda tg_id: (_ for _ in ()).throw(billing.access.AccessError("down")),
    )
    job = SimpleNamespace(
        id=102,
        payload={
            "subscription_id": subscription.id,
            "tg_id": TG_ID,
            "first_attempt_at": (NOW - timedelta(hours=25)).isoformat(),
        },
    )

    billing.handle_kick(session, job)

    assert session.query(Job).filter(Job.type == JobType.kick).count() == 0


def test_reconcile_handle_schedules_next_hour(session, monkeypatch):
    monkeypatch.setattr(reconcile, "_now", lambda: NOW)
    job = jobs.enqueue(session, JobType.reconcile.value, NOW, {})
    session.flush()

    reconcile.handle_job(session, job)

    next_jobs = session.query(Job).filter(Job.type == JobType.reconcile, Job.id != job.id).all()
    assert len(next_jobs) == 1
    assert next_jobs[0].run_at == NOW + timedelta(hours=1)


def test_bootstrap_creates_single_reconcile_job(session):
    first = billing_worker.bootstrap(NOW)
    second = billing_worker.bootstrap(NOW + timedelta(minutes=5))

    assert first is not None and second is not None
    assert first.id == second.id
    assert session.query(Job).filter(Job.type == JobType.reconcile).count() == 1
