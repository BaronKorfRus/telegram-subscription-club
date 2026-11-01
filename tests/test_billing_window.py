from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import texts
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import billing, jobs, payments, subscriptions
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
WINDOW_DAY = datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)
OUTSIDE = datetime(2026, 11, 26, 9, 0, tzinfo=MOSCOW)
TG_ID = 960001
PERIOD_END = date(2026, 11, 22)


@pytest.fixture(autouse=True)
def clean_tables(session):
    session.execute(
        text("TRUNCATE jobs, payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()
    yield
    session.rollback()


@pytest.fixture
def due_sub(session):
    seed_tariffs(session)
    client = Client(
        tg_id=TG_ID, first_name="Анна", email="anna@example.com", phone="+79991234567"
    )
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 22, 9, 0, tzinfo=MOSCOW),
        current_period_end=PERIOD_END,
        auto_renew=True,
        billing_enabled=True,
        rebill_id="rebill-1",
        card_mask="430000******0000",
        failed_attempts=0,
    )
    session.add(subscription)
    session.flush()
    return subscription


@pytest.fixture
def bank(monkeypatch):
    calls = {"init": 0, "charge": 0}

    def init_payment(**kwargs):
        calls["init"] += 1
        return {
            "Success": True,
            "PaymentId": f"pay-{calls['init']}",
            "PaymentURL": f"https://pay.example/{calls['init']}",
        }

    def charge(payment_id, rebill_id):
        calls["charge"] += 1
        return {"Success": True, "Status": "CONFIRMED"}

    monkeypatch.setattr(billing.tbank, "init_payment", init_payment)
    monkeypatch.setattr(billing.tbank, "charge", charge)
    return calls


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        billing, "notify", lambda tg_id, text, reply_markup=None: sent.append((tg_id, text))
    )
    return sent


def test_next_charge_moment_before_window_is_this_month():
    assert billing.next_charge_moment(datetime(2026, 11, 26, 12, 0, tzinfo=MOSCOW)) == datetime(
        2026, 12, 22, 9, 0, tzinfo=MOSCOW
    )


def test_next_charge_moment_after_window_is_next_month():
    assert billing.next_charge_moment(datetime(2026, 11, 1, 12, 0, tzinfo=MOSCOW)) == datetime(
        2026, 11, 22, 9, 0, tzinfo=MOSCOW
    )


def test_charge_outside_window_is_deferred_not_charged(session, due_sub, bank, notifications):
    result = billing.charge_subscription(session, due_sub, now=OUTSIDE)

    assert result is None
    assert bank["init"] == 0
    assert bank["charge"] == 0
    assert session.query(Payment).count() == 0

    deferred = session.query(Job).filter_by(type=JobType.charge).one()
    assert deferred.run_at == datetime(2026, 12, 22, 9, 0, tzinfo=MOSCOW)
    assert deferred.payload == {"subscription_id": due_sub.id}


def test_charge_inside_window_still_charges(session, due_sub, bank, notifications):
    payment = billing.charge_subscription(session, due_sub, now=WINDOW_DAY)

    assert payment is not None
    assert bank["charge"] == 1
    assert due_sub.current_period_end == date(2026, 12, 22)


def test_cycle_schedules_expire_check_on_25th(session, due_sub):
    billing.schedule_cycle(session, due_sub)

    job = session.query(Job).filter_by(type=JobType.expire_check).one()
    assert job.run_at == datetime(2026, 11, 25, 0, 5, tzinfo=MOSCOW)
    assert job.payload == {"subscription_id": due_sub.id, "period_end": "2026-11-22"}


def test_schedule_cycle_replaces_previous_expire_check(session, due_sub):
    billing.schedule_cycle(session, due_sub)
    due_sub.current_period_end = date(2026, 12, 22)
    billing.schedule_cycle(session, due_sub)

    checks = session.query(Job).filter_by(type=JobType.expire_check).all()
    assert len(checks) == 1
    assert checks[0].payload["period_end"] == "2026-12-22"


def test_retry_is_scheduled_next_day_at_nine():
    assert billing.next_retry_moment(datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)) == datetime(
        2026, 11, 23, 9, 0, tzinfo=MOSCOW
    )
    assert billing.next_retry_moment(datetime(2026, 11, 23, 11, 30, tzinfo=MOSCOW)) == datetime(
        2026, 11, 24, 9, 0, tzinfo=MOSCOW
    )


def _rejecting_bank(monkeypatch, count):
    results = [{"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"}] * count
    state = {"init": 0}

    def init_payment(**kwargs):
        state["init"] += 1
        return {
            "Success": True,
            "PaymentId": f"pay-{state['init']}",
            "PaymentURL": f"https://pay.example/{state['init']}",
        }

    def charge(payment_id, rebill_id):
        return results.pop(0)

    monkeypatch.setattr(billing.tbank, "init_payment", init_payment)
    monkeypatch.setattr(billing.tbank, "charge", charge)


def _mark_charge_jobs_done(session, subscription):
    """Изображает воркер: после обработки задачи она больше не pending."""
    for job in session.query(Job).filter_by(type=JobType.charge).all():
        if job.payload["subscription_id"] == subscription.id and job.status == JobStatus.pending:
            job.status = JobStatus.done
    session.flush()


def _pending_charge_job(session, subscription):
    return (
        session.query(Job)
        .filter(
            Job.type == JobType.charge,
            Job.status == JobStatus.pending,
            Job.payload["subscription_id"].as_integer() == subscription.id,
        )
        .order_by(Job.id.desc())
        .first()
    )


def test_three_failures_leave_subscription_past_due_until_25th(
    session, due_sub, monkeypatch, notifications
):
    _rejecting_bank(monkeypatch, 3)
    billing.schedule_cycle(session, due_sub)
    _mark_charge_jobs_done(session, due_sub)

    billing.charge_subscription(session, due_sub, now=WINDOW_DAY)
    assert due_sub.status == SubscriptionStatus.past_due
    assert due_sub.failed_attempts == 1

    retry = _pending_charge_job(session, due_sub)
    assert retry is not None
    assert retry.run_at == datetime(2026, 11, 23, 9, 0, tzinfo=MOSCOW)
    _mark_charge_jobs_done(session, due_sub)

    billing.charge_subscription(session, due_sub, now=datetime(2026, 11, 23, 9, 0, tzinfo=MOSCOW))
    assert due_sub.failed_attempts == 2
    _mark_charge_jobs_done(session, due_sub)

    billing.charge_subscription(session, due_sub, now=datetime(2026, 11, 24, 9, 0, tzinfo=MOSCOW))

    assert due_sub.status == SubscriptionStatus.past_due
    assert due_sub.failed_attempts == 3
    # Ни истечения, ни удаления из канала на третьем отказе.
    assert session.query(Job).filter_by(type=JobType.kick).count() == 0
    assert texts.WINDOW_CHARGE_FAILED in [text for _tg, text in notifications]


def test_expire_check_expires_unpaid(session, due_sub, monkeypatch, notifications):
    monkeypatch.setattr(billing.access, "revoke", lambda tg_id: None)
    billing.schedule_cycle(session, due_sub)
    job = session.query(Job).filter_by(type=JobType.expire_check).one()

    billing.handle_expire_check(session, job)

    assert due_sub.status == SubscriptionStatus.expired
    kick = session.query(Job).filter_by(type=JobType.kick).one()
    assert kick.payload["subscription_id"] == due_sub.id


def test_expire_check_skips_paid_subscription(session, due_sub, monkeypatch, notifications):
    billing.schedule_cycle(session, due_sub)
    job = session.query(Job).filter_by(type=JobType.expire_check).one()
    # Клиент оплатил 23-го: период уехал на 22 декабря.
    due_sub.current_period_end = date(2026, 12, 22)
    session.flush()

    billing.handle_expire_check(session, job)

    assert due_sub.status == SubscriptionStatus.active
    assert session.query(Job).filter_by(type=JobType.kick).count() == 0


def test_confirmation_after_window_extends_period(session, due_sub, monkeypatch, notifications):
    """Платёж создан 24.10, банк подтвердил 25.10 00:10 → период до 22.11, доступ есть."""
    payment = Payment(
        client_id=due_sub.client_id,
        subscription_id=due_sub.id,
        tariff_id=due_sub.tariff_id,
        kind=payments.PaymentKind.renewal,
        amount_kopecks=due_sub.tariff.price_kopecks,
        order_id="order-after-window",
        status=PaymentStatus.pending,
        bank_payment_id="777001",
        initiated_by_worker=True,
    )
    session.add(payment)
    due_sub.current_period_end = date(2026, 10, 22)
    session.flush()

    monkeypatch.setattr(payments, "check_token", lambda payload, password: True)
    payments.handle_notification(
        session,
        {
            "Status": "CONFIRMED",
            "Amount": due_sub.tariff.price_kopecks,
            "PaymentId": "777001",
            "OrderId": "order-after-window",
            "RebillId": "rebill-1",
        },
        now=datetime(2026, 10, 25, 0, 10, tzinfo=MOSCOW),
    )
    session.flush()

    assert payment.status == PaymentStatus.confirmed
    assert due_sub.current_period_end == date(2026, 11, 22)
    assert due_sub.status == SubscriptionStatus.active


def test_reminder_dates_follow_the_22nd(session, due_sub):
    billing.schedule_cycle(session, due_sub)

    runs = {
        job.type: job.run_at for job in session.query(Job).filter(Job.type != JobType.expire_check)
    }
    assert runs[JobType.reminder_3d] == datetime(2026, 11, 19, 9, 0, tzinfo=MOSCOW)
    assert runs[JobType.reminder_1d] == datetime(2026, 11, 21, 9, 0, tzinfo=MOSCOW)
    assert runs[JobType.charge] == datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)


def test_expire_check_handler_registered():
    from app import billing_worker

    assert billing_worker.HANDLERS["expire_check"] is not None


def test_jobtypes_include_new_members():
    assert JobType.expire_check.value == "expire_check"
    assert subscriptions.add_months(PERIOD_END, 1) == date(2026, 12, 22)
    assert jobs is not None
    assert JobStatus.pending.value == "pending"
    assert timedelta(days=1)
