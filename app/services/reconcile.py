import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tbank
from app.config import settings
from app.models import Job, JobStatus, JobType, Payment, PaymentKind, PaymentStatus
from app.services import billing, jobs, post_payment, subscriptions

logger = logging.getLogger(__name__)

RECONCILE_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _confirm(session: Session, payment: Payment, now: datetime) -> bool:
    subscription = payment.subscription
    if payment.kind == PaymentKind.initial:
        if payment.tariff is None:
            logger.error("reconcile: initial payment %s без тарифа", payment.id)
            return False
        subscription = subscriptions.activate(session, payment.client, payment.tariff, payment, now)
        billing.schedule_cycle(session, subscription)
    elif payment.kind == PaymentKind.renewal:
        if subscription is None:
            logger.error("reconcile: renewal payment %s без подписки", payment.id)
            return False
        subscriptions.extend(session, subscription, payment, now)
        billing.schedule_cycle(session, subscription)

    payment.status = PaymentStatus.confirmed
    payment.confirmed_at = now
    payment.error_code = None
    payment.error_message = None
    if subscription is not None and payment.kind in (
        PaymentKind.initial,
        PaymentKind.renewal,
    ):
        post_payment.enqueue(session, payment, now)
    session.flush()
    return True


def _reject(session: Session, payment: Payment, state: dict, now: datetime) -> None:
    if payment.subscription is not None:
        billing.record_failure(session, payment.subscription, payment, state, now)
        return
    payment.status = PaymentStatus.rejected
    error_code = state.get("ErrorCode")
    payment.error_code = str(error_code) if error_code is not None else None
    payment.error_message = str(state.get("Message") or state.get("Details") or "Rejected by bank")
    session.flush()


def _process_payment(session: Session, payment: Payment, now: datetime) -> bool:
    if payment.status != PaymentStatus.pending or not payment.bank_payment_id:
        return False
    try:
        state = tbank.get_state(payment.bank_payment_id)
    except tbank.TbankError:
        logger.exception("reconcile: GetState failed for payment %s", payment.id)
        return False

    status = str(state.get("Status", "")).upper()
    if status == "CONFIRMED":
        return _confirm(session, payment, now)
    if status in {"REJECTED", "CANCELED", "CANCELLED"}:
        _reject(session, payment, state, now)
        return True
    return False


def run(session: Session, now: datetime | None = None) -> int:
    """GetState по pending-платежам старше 15 минут."""
    now = now or _now()
    cutoff = now - RECONCILE_AFTER
    payments = session.scalars(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.pending,
            Payment.bank_payment_id.is_not(None),
            Payment.created_at <= cutoff,
        )
        .order_by(Payment.id)
    )
    processed = 0
    for payment in payments:
        if _process_payment(session, payment, now):
            processed += 1
    session.flush()
    return processed


def schedule_next(
    session: Session,
    now: datetime | None = None,
    exclude_job_id: int | None = None,
) -> Job:
    """Следующая сверка: одна pending/running задача, без дублей."""
    now = now or _now()
    conditions = [
        Job.type == JobType.reconcile,
        Job.status.in_((JobStatus.pending, JobStatus.running)),
    ]
    if exclude_job_id is not None:
        conditions.append(Job.id != exclude_job_id)
    existing = session.scalar(select(Job).where(*conditions))
    if existing is not None:
        return existing
    return jobs.enqueue(session, JobType.reconcile.value, now, {})


def handle_job(session: Session, job: Job) -> None:
    now = _now()
    payment_id = (job.payload or {}).get("payment_id")
    if not payment_id:
        run(session, now)
    else:
        payment = session.get(Payment, payment_id)
        if payment is None:
            logger.warning("reconcile: платёж %s не найден", payment_id)
        else:
            _process_payment(session, payment, now)

    schedule_next(session, now + timedelta(hours=1), exclude_job_id=job.id)
    session.flush()
