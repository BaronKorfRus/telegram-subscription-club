from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import billing_worker, tbank, texts
from app.db import SessionLocal
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Message,
    MessageKind,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import billing, jobs, post_payment
from app.services.billing import RetryLater, charge_subscription
from sqlalchemy import select, text

MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=MOSCOW)
TG_ID = 950001


@pytest.fixture(autouse=True)
def clean_billing_tables(session):
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
def due_sub(session):
    seed_tariffs(session)
    client = Client(
        tg_id=TG_ID,
        first_name="Анна",
        email="anna@example.com",
        phone="+79991234567",
    )
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 9, 23, 12, 0, tzinfo=MOSCOW),
        current_period_end=TODAY,
        auto_renew=True,
        billing_enabled=True,
        rebill_id="rebill-1",
        card_mask="430000******0000",
        failed_attempts=0,
    )
    session.add(subscription)
    session.flush()
    return subscription


class FakeBank:
    def __init__(self, charge_results=None, state_results=None, init_result=None):
        self.init_calls = []
        self.charge_calls = 0
        self.charge_payment_ids = []
        self.get_state_calls = 0
        self.charge_results = list(charge_results or [])
        self.state_results = list(state_results or [])
        self.init_result = init_result
        self.init_observed = []

    def init_payment(self, **kwargs):
        self.init_calls.append(kwargs)
        with SessionLocal() as other:
            row = other.scalar(select(Payment).where(Payment.order_id == kwargs["order_id"]))
            self.init_observed.append(
                (row is not None, row.status if row else None, row.bank_payment_id if row else None)
            )
        result = dict(self.init_result) if self.init_result else {"Success": True}
        result.setdefault("PaymentId", f"pay-{len(self.init_calls)}")
        result.setdefault("PaymentURL", f"https://pay.example/{len(self.init_calls)}")
        return result

    def charge(self, payment_id, rebill_id):
        self.charge_calls += 1
        self.charge_payment_ids.append(payment_id)
        result = self.charge_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_state(self, payment_id):
        self.get_state_calls += 1
        result = self.state_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def bank(monkeypatch):
    fake = FakeBank(charge_results=[{"Success": True, "Status": "CONFIRMED"}])
    monkeypatch.setattr(billing.tbank, "init_payment", fake.init_payment)
    monkeypatch.setattr(billing.tbank, "charge", fake.charge)
    monkeypatch.setattr(billing.tbank, "get_state", fake.get_state)
    return fake


@pytest.fixture
def post_notifications(monkeypatch):
    sent = []

    def fake_notify(tg_id, text, reply_markup=None):
        sent.append((tg_id, text))
        return True

    monkeypatch.setattr(post_payment, "notify", fake_notify)
    return sent


@pytest.fixture
def grants(monkeypatch):
    calls = []
    monkeypatch.setattr(post_payment.access, "grant", lambda tg_id: calls.append(tg_id))
    return calls


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        billing, "notify", lambda tg_id, text, reply_markup=None: sent.append((tg_id, text))
    )
    return sent


def test_charge_extends_and_schedules_next_cycle(session, due_sub, bank):
    payment = charge_subscription(session, due_sub, now=NOW)

    assert payment is not None
    assert payment.status == PaymentStatus.confirmed
    assert due_sub.status == SubscriptionStatus.active
    assert due_sub.current_period_end == date(2026, 11, 22)
    assert due_sub.failed_attempts == 0
    assert bank.init_calls[0]["recurrent"] is False
    assert bank.init_calls[0]["extra"] == {"OperationInitiatorType": "R"}
    assert bank.init_calls[0]["amount_kopecks"] == due_sub.tariff.price_kopecks
    assert bank.charge_calls == 1

    cycle = session.query(Job).filter(Job.type != JobType.post_payment).order_by(Job.run_at).all()
    assert {job.type for job in cycle} == {
        JobType.reminder_3d,
        JobType.reminder_1d,
        JobType.charge,
    }
    runs = {job.type: job.run_at for job in cycle}
    assert runs[JobType.reminder_3d] == datetime(2026, 11, 19, 9, 0, tzinfo=MOSCOW)
    assert runs[JobType.reminder_1d] == datetime(2026, 11, 21, 9, 0, tzinfo=MOSCOW)
    assert runs[JobType.charge] == datetime(2026, 11, 22, 9, 0, tzinfo=MOSCOW)
    assert all(job.payload == {"subscription_id": due_sub.id} for job in cycle)
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1


def test_payment_row_exists_before_init(session, due_sub, bank, notifications):
    charge_subscription(session, due_sub, now=NOW)

    assert bank.init_observed == [(True, PaymentStatus.created, None)]


def test_timeout_then_get_state_prevents_duplicate(session, due_sub, monkeypatch, notifications):
    fake = FakeBank(
        charge_results=[tbank.TbankError("timeout")],
        state_results=[
            {"Success": True, "Status": "PENDING"},
            {"Success": True, "Status": "CONFIRMED"},
        ],
    )
    monkeypatch.setattr(billing.tbank, "init_payment", fake.init_payment)
    monkeypatch.setattr(billing.tbank, "charge", fake.charge)
    monkeypatch.setattr(billing.tbank, "get_state", fake.get_state)

    with pytest.raises(RetryLater):
        charge_subscription(session, due_sub, now=NOW)

    reconcile = session.query(Job).filter_by(type=JobType.reconcile).one()
    assert reconcile.payload["bank_payment_id"] == "pay-1"

    payment = charge_subscription(session, due_sub, now=NOW + timedelta(minutes=20))

    assert payment is not None
    assert payment.status == PaymentStatus.confirmed
    assert fake.charge_calls == 1
    assert len(fake.init_calls) == 1
    assert fake.get_state_calls == 2
    assert due_sub.current_period_end == date(2026, 11, 22)


def test_three_failures_expire_and_schedule_kick(session, due_sub, monkeypatch, notifications):
    fake = FakeBank(
        charge_results=[
            {"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"},
            {"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"},
            {"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"},
        ]
    )
    monkeypatch.setattr(billing.tbank, "init_payment", fake.init_payment)
    monkeypatch.setattr(billing.tbank, "charge", fake.charge)
    monkeypatch.setattr(billing.tbank, "get_state", fake.get_state)

    charge_subscription(session, due_sub, now=NOW)
    assert due_sub.status == SubscriptionStatus.past_due
    assert due_sub.failed_attempts == 1
    retry = session.query(Job).filter_by(type=JobType.charge).one()
    assert retry.run_at == NOW + timedelta(hours=2)
    assert "через 2 часа" in notifications[-1][1]

    charge_subscription(session, due_sub, now=NOW + timedelta(hours=2))
    assert due_sub.failed_attempts == 2
    retries = session.query(Job).filter_by(type=JobType.charge).order_by(Job.id).all()
    assert retries[-1].run_at == NOW + timedelta(hours=26)
    assert "через 24 часа" in notifications[-1][1]

    charge_subscription(session, due_sub, now=NOW + timedelta(hours=26))
    assert due_sub.status == SubscriptionStatus.expired
    assert due_sub.failed_attempts == 3
    kick = session.query(Job).filter_by(type=JobType.kick).one()
    assert kick.payload["subscription_id"] == due_sub.id
    assert "подписка окончена" in notifications[-1][1]

    rejected = session.query(Payment).filter_by(status=PaymentStatus.rejected).all()
    assert len(rejected) == 3
    assert all(payment.error_code == "7" for payment in rejected)


def test_billing_disabled_skips_charge(session, due_sub, bank, notifications):
    due_sub.billing_enabled = False

    assert charge_subscription(session, due_sub, now=NOW) is None
    assert bank.init_calls == []
    assert bank.charge_calls == 0
    assert session.query(Payment).count() == 0
    assert notifications == []


@pytest.mark.parametrize("field", ["auto_renew"])
def test_disabled_renewal_skips_charge(session, due_sub, bank, notifications, field):
    setattr(due_sub, field, False)

    assert charge_subscription(session, due_sub, now=NOW) is None
    assert bank.init_calls == []


def test_reminder_jobs_send_salebot_texts(session, due_sub, notifications):
    job_3d = jobs.enqueue(session, JobType.reminder_3d.value, NOW, {"subscription_id": due_sub.id})
    billing.handle_job(session, job_3d)
    assert notifications[-1][1] == texts.REMINDER_3D[due_sub.tariff.price_kopecks]

    job_1d = jobs.enqueue(session, JobType.reminder_1d.value, NOW, {"subscription_id": due_sub.id})
    billing.handle_job(session, job_1d)
    assert notifications[-1][1] == texts.REMINDER_1D[due_sub.tariff.price_kopecks]


def test_billing_disabled_skips_reminders(session, due_sub, notifications):
    due_sub.billing_enabled = False
    job = jobs.enqueue(session, JobType.reminder_1d.value, NOW, {"subscription_id": due_sub.id})

    billing.handle_job(session, job)

    assert notifications == []


def test_worker_processes_charge_job(session, due_sub, bank, notifications, monkeypatch):
    monkeypatch.setattr(billing, "_now", lambda: NOW)
    job = jobs.enqueue(session, JobType.charge.value, NOW, {"subscription_id": due_sub.id})
    session.flush()
    job.status = JobStatus.running
    job.locked_until = NOW + timedelta(minutes=5)
    session.flush()

    billing_worker.process_claimed(session, [job])

    session.expire_all()
    stored = session.get(Job, job.id)
    assert stored.status == JobStatus.done
    assert due_sub.current_period_end == date(2026, 11, 22)


def test_post_payment_grants_and_notifies_once(session, due_sub, bank, post_notifications, grants):
    payment = charge_subscription(session, due_sub, now=NOW)
    job = session.query(Job).filter_by(type=JobType.post_payment).one()

    post_payment.handle(session, job)

    assert grants == [TG_ID]
    assert len(post_notifications) == 1
    assert "Подписка активна до 22.11.2026" in post_notifications[0][1]
    outgoing = session.query(Message).filter_by(kind=MessageKind.bot).one()
    assert "Подписка активна до 22.11.2026" in outgoing.text
    assert payment.post_payment_done is True

    # Повторная обработка того же payment_id и повторный enqueue ничего не дублируют.
    post_payment.handle(session, job)
    post_payment.enqueue(session, payment)

    assert grants == [TG_ID]
    assert len(post_notifications) == 1
    assert session.query(Job).filter_by(type=JobType.post_payment).count() == 1
    assert session.query(Message).filter_by(kind=MessageKind.bot).count() == 1


def test_schedule_cycle_is_idempotent(session, due_sub):
    billing.schedule_cycle(session, due_sub)
    billing.schedule_cycle(session, due_sub)

    assert session.query(Job).count() == 3


def test_post_payment_retries_after_grant_failure(
    session, due_sub, bank, post_notifications, monkeypatch
):
    calls = []

    def grant(tg_id):
        calls.append(tg_id)
        if len(calls) == 1:
            raise post_payment.access.AccessError("telegram down")

    monkeypatch.setattr(post_payment.access, "grant", grant)
    monkeypatch.setattr(post_payment, "_now", lambda: NOW)
    payment = charge_subscription(session, due_sub, now=NOW)
    job = session.query(Job).filter_by(type=JobType.post_payment).one()

    post_payment.handle(session, job)

    assert payment.post_payment_done is False
    assert post_notifications == []
    retry = session.query(Job).filter(Job.type == JobType.post_payment, Job.id != job.id).one()
    assert retry.run_at == NOW + timedelta(minutes=5)

    post_payment.handle(session, retry)

    assert payment.post_payment_done is True
    assert len(calls) == 2
    assert len(post_notifications) == 1


def test_post_payment_retries_after_message_failure(session, due_sub, bank, monkeypatch):
    attempts = []

    def fake_notify(tg_id, text, reply_markup=None):
        attempts.append(text)
        return len(attempts) > 1

    monkeypatch.setattr(post_payment, "notify", fake_notify)
    monkeypatch.setattr(post_payment.access, "grant", lambda tg_id: None)
    monkeypatch.setattr(post_payment, "_now", lambda: NOW)
    payment = charge_subscription(session, due_sub, now=NOW)
    job = session.query(Job).filter_by(type=JobType.post_payment).one()

    post_payment.handle(session, job)
    assert payment.post_payment_done is False
    retry = session.query(Job).filter(Job.type == JobType.post_payment, Job.id != job.id).one()

    post_payment.handle(session, retry)

    assert payment.post_payment_done is True
    assert len(attempts) == 2


def test_post_payment_gives_up_after_24h(session, due_sub, bank, post_notifications, monkeypatch):
    def grant(tg_id):
        raise post_payment.access.AccessError("telegram down")

    monkeypatch.setattr(post_payment.access, "grant", grant)
    monkeypatch.setattr(post_payment, "_now", lambda: NOW)
    charge_subscription(session, due_sub, now=NOW)
    job = session.query(Job).filter_by(type=JobType.post_payment).one()
    job.payload = {
        **job.payload,
        "first_attempt_at": (NOW - timedelta(hours=25)).isoformat(),
    }
    session.flush()

    post_payment.handle(session, job)

    assert session.query(Job).filter(Job.type == JobType.post_payment).count() == 1


def test_init_network_error_retries_without_failure(session, due_sub, monkeypatch, notifications):
    def init(**kwargs):
        raise tbank.TbankError("timeout")

    monkeypatch.setattr(billing.tbank, "init_payment", init)

    payment = charge_subscription(session, due_sub, now=NOW)

    assert payment is not None
    assert payment.status == PaymentStatus.rejected
    assert payment.error_code == "network"
    assert due_sub.failed_attempts == 0
    assert due_sub.status == SubscriptionStatus.active
    assert notifications == []
    retry = session.query(Job).filter_by(type=JobType.charge).one()
    assert retry.run_at == NOW + timedelta(minutes=15)


def test_reconcile_new_status_sends_charge_for_existing_payment(
    session, due_sub, monkeypatch, notifications
):
    payment = Payment(
        client_id=due_sub.client_id,
        subscription_id=due_sub.id,
        tariff_id=due_sub.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=due_sub.tariff.price_kopecks,
        order_id="order-pending-new",
        bank_payment_id="pay-pending-new",
        status=PaymentStatus.pending,
        initiated_by_worker=True,
    )
    session.add(payment)
    session.flush()
    fake = FakeBank(
        charge_results=[{"Success": True, "Status": "CONFIRMED"}],
        state_results=[{"Success": True, "Status": "NEW"}],
    )
    monkeypatch.setattr(billing.tbank, "init_payment", fake.init_payment)
    monkeypatch.setattr(billing.tbank, "charge", fake.charge)
    monkeypatch.setattr(billing.tbank, "get_state", fake.get_state)

    result = charge_subscription(session, due_sub, now=NOW)

    assert fake.charge_calls == 1
    assert fake.init_calls == []
    assert result is not None and result.status == PaymentStatus.confirmed
    assert due_sub.current_period_end == date(2026, 11, 22)


def test_pending_card_change_does_not_block_renewal(session, due_sub, bank):
    card_change = Payment(
        client_id=due_sub.client_id,
        subscription_id=due_sub.id,
        tariff_id=due_sub.tariff_id,
        kind=PaymentKind.card_change,
        amount_kopecks=100,
        order_id="order-card-pending",
        bank_payment_id="card-pending",
        status=PaymentStatus.pending,
    )
    session.add(card_change)
    session.flush()

    payment = charge_subscription(session, due_sub, now=NOW)

    assert payment is not None and payment.kind == PaymentKind.renewal
    assert card_change.status == PaymentStatus.pending
    assert bank.init_calls
    assert bank.charge_calls == 1


def test_manual_pending_renewal_is_not_charged_by_worker(session, due_sub, bank):
    manual = Payment(
        client_id=due_sub.client_id,
        subscription_id=due_sub.id,
        tariff_id=due_sub.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=due_sub.tariff.price_kopecks,
        order_id="order-manual-pending",
        bank_payment_id="manual-pending",
        status=PaymentStatus.pending,
        initiated_by_worker=False,
    )
    session.add(manual)
    session.flush()

    payment = charge_subscription(session, due_sub, now=NOW)

    assert payment is not None and payment.initiated_by_worker is True
    assert manual.status == PaymentStatus.pending
    assert "manual-pending" not in bank.charge_payment_ids
    assert bank.get_state_calls == 0
    assert bank.init_calls


def test_worker_pending_renewal_is_charged_by_existing_payment(session, due_sub, monkeypatch):
    worker_payment = Payment(
        client_id=due_sub.client_id,
        subscription_id=due_sub.id,
        tariff_id=due_sub.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=due_sub.tariff.price_kopecks,
        order_id="order-worker-pending",
        bank_payment_id="worker-pending",
        status=PaymentStatus.pending,
        initiated_by_worker=True,
    )
    session.add(worker_payment)
    session.flush()
    fake = FakeBank(
        charge_results=[{"Success": True, "Status": "CONFIRMED"}],
        state_results=[{"Success": True, "Status": "NEW"}],
    )
    monkeypatch.setattr(billing.tbank, "init_payment", fake.init_payment)
    monkeypatch.setattr(billing.tbank, "charge", fake.charge)
    monkeypatch.setattr(billing.tbank, "get_state", fake.get_state)

    payment = charge_subscription(session, due_sub, now=NOW)

    assert payment is worker_payment
    assert fake.init_calls == []
    assert fake.get_state_calls == 1
    assert fake.charge_payment_ids == ["worker-pending"]
    assert worker_payment.status == PaymentStatus.confirmed
