import logging
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import receipts, reports, tbank, telegram, texts
from app.config import settings
from app.models import (
    Job,
    JobStatus,
    JobType,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
)
from app.services import access, jobs, payments, post_payment, subscriptions
from app.services import messages as messages_service

logger = logging.getLogger(__name__)

RETRY_1 = timedelta(hours=2)
RETRY_2 = timedelta(hours=24)
RECONCILE_DELAY = timedelta(minutes=15)
INIT_RETRY_DELAY = timedelta(minutes=15)
KICK_RETRY_DELAY = timedelta(minutes=5)
KICK_RETRY_WINDOW = timedelta(hours=24)


class RetryLater(RuntimeError):
    """Банк не дал однозначного ответа; ждём reconcile, Init не повторяем."""


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _now() -> datetime:
    return datetime.now(_tz())


def _run_at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=_tz())


def _format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _client_name(subscription: Subscription) -> str:
    client = subscription.client
    return client.first_name or client.username or "Здравствуйте"


def _type_value(job_type: JobType | str) -> str:
    return job_type.value if isinstance(job_type, JobType) else str(job_type)


def notify(tg_id: int, text: str, reply_markup: dict | None = None) -> None:
    params: dict = {"chat_id": tg_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        telegram.call("sendMessage", params)
    except telegram.TelegramError as exc:
        logger.exception("Не удалось отправить сообщение клиенту tg_id=%s", tg_id)
        messages_service.record_delivery_error(tg_id, text, exc)


def _reminder_text(job_type: str, subscription: Subscription) -> str | None:
    if subscription.tariff is None:
        return None
    price = subscription.tariff.price_kopecks
    if job_type == JobType.reminder_3d.value:
        return texts.REMINDER_3D.get(price) or texts.REMINDER_3D_TEMPLATE.format(
            amount=price // 100
        )
    if job_type == JobType.reminder_1d.value:
        return texts.REMINDER_1D.get(price) or texts.REMINDER_1D_TEMPLATE.format(
            amount=price // 100
        )
    return None


def schedule_cycle(session: Session, subscription: Subscription) -> None:
    """Напоминания за 3/1 день и списание — в 09:00 МСК."""
    end = subscription.current_period_end
    cycle = (
        (JobType.reminder_3d.value, end - timedelta(days=3)),
        (JobType.reminder_1d.value, end - timedelta(days=1)),
        (JobType.charge.value, end),
    )
    for job_type, day in cycle:
        # Идемпотентность: старые pending-задачи этого типа заменяем новыми.
        session.execute(
            delete(Job).where(
                Job.type == job_type,
                Job.status == JobStatus.pending,
                Job.payload["subscription_id"].as_integer() == subscription.id,
            )
        )
        jobs.enqueue(
            session,
            job_type,
            _run_at(day),
            {"subscription_id": subscription.id},
        )
    session.flush()


def _schedule_reconcile(session: Session, payment: Payment, now: datetime) -> None:
    jobs.enqueue(
        session,
        JobType.reconcile.value,
        now + RECONCILE_DELAY,
        {"payment_id": payment.id, "bank_payment_id": payment.bank_payment_id},
    )
    session.flush()


def expire(session: Session, subscription: Subscription, now: datetime | None = None) -> Job | None:
    """Истечение: статус expired, сообщение и задача kick."""
    now = now or _now()
    already_expired = subscription.status == SubscriptionStatus.expired
    subscription.status = SubscriptionStatus.expired
    subscription.auto_renew = False
    job = None
    if not already_expired:
        job = jobs.enqueue(
            session,
            JobType.kick.value,
            now,
            {
                "subscription_id": subscription.id,
                "tg_id": subscription.client.tg_id,
            },
        )
        notify(
            subscription.client.tg_id,
            texts.SUBSCRIPTION_EXPIRED.format(name=_client_name(subscription)),
        )
    session.flush()
    return job


def _schedule_kick_retry(
    session: Session, subscription: Subscription, job: Job, now: datetime
) -> Job | None:
    first_raw = (job.payload or {}).get("first_attempt_at")
    try:
        first_attempt = datetime.fromisoformat(first_raw) if first_raw else now
    except ValueError:
        first_attempt = now
    if first_attempt.tzinfo is None:
        first_attempt = first_attempt.replace(tzinfo=_tz())
    if now - first_attempt >= KICK_RETRY_WINDOW:
        logger.error("kick: истекли 24 часа повторов sub=%s", subscription.id)
        return None

    conditions = [
        Job.type == JobType.kick,
        Job.payload["subscription_id"].as_integer() == subscription.id,
        Job.status.in_((JobStatus.pending, JobStatus.running)),
    ]
    if getattr(job, "id", None) is not None:
        conditions.append(Job.id != job.id)
    existing = session.scalar(select(Job).where(*conditions))
    if existing is not None:
        return existing

    return jobs.enqueue(
        session,
        JobType.kick.value,
        now + KICK_RETRY_DELAY,
        {
            "subscription_id": subscription.id,
            "tg_id": subscription.client.tg_id,
            "first_attempt_at": first_attempt.isoformat(),
        },
    )


def handle_kick(session: Session, job: Job) -> None:
    subscription_id = (job.payload or {}).get("subscription_id")
    subscription = session.get(Subscription, subscription_id) if subscription_id else None
    if subscription is None:
        logger.warning("kick: подписка %s не найдена", subscription_id)
        return

    now = _now()
    today = now.date()
    if subscription.current_period_end > today or subscription.auto_renew:
        logger.info(
            "kick пропущен: sub=%s end=%s auto_renew=%s",
            subscription.id,
            subscription.current_period_end,
            subscription.auto_renew,
        )
        return

    try:
        access.revoke(subscription.client.tg_id)
    except access.AccessError:
        logger.exception("kick: не удалось удалить из каналов sub=%s", subscription.id)
        _schedule_kick_retry(session, subscription, job, now)
        return

    notify(
        subscription.client.tg_id,
        texts.SUBSCRIPTION_EXPIRED.format(name=_client_name(subscription)),
    )


def record_failure(
    session: Session,
    subscription: Subscription,
    payment: Payment,
    payload: dict,
    now: datetime,
) -> None:
    payment.status = PaymentStatus.rejected
    error_code = payload.get("ErrorCode")
    payment.error_code = str(error_code) if error_code is not None else None
    payment.error_message = str(
        payload.get("Message") or payload.get("Details") or "Charge rejected"
    )

    subscription.failed_attempts += 1
    attempts = subscription.failed_attempts
    session.flush()

    if attempts >= 3:
        expire(session, subscription, now)
    else:
        subscription.status = SubscriptionStatus.past_due
        delay = RETRY_1 if attempts == 1 else RETRY_2
        jobs.enqueue(
            session,
            JobType.charge.value,
            now + delay,
            {"subscription_id": subscription.id},
        )
        text = texts.CHARGE_FAILED_2H if attempts == 1 else texts.CHARGE_FAILED_24H
        notify(subscription.client.tg_id, text)
    session.flush()


def handle_payment_failed(session: Session, job: Job) -> None:
    """Уведомление о неудачной ручной оплате; не меняет попытки и не ретраит."""
    payment_id = (job.payload or {}).get("payment_id")
    payment = session.get(Payment, payment_id) if payment_id else None
    if payment is None or payment.client is None:
        logger.warning("payment_failed: платёж %s не найден", payment_id)
        return
    notify(payment.client.tg_id, texts.PAYMENT_FAILED)


def _succeed(
    session: Session,
    subscription: Subscription,
    payment: Payment,
    now: datetime,
) -> None:
    payment.status = PaymentStatus.confirmed
    payment.confirmed_at = now
    payment.error_code = None
    payment.error_message = None
    subscriptions.extend(session, subscription, payment, now)
    schedule_cycle(session, subscription)
    session.flush()
    post_payment.enqueue(session, payment, now)


def _reconcile(
    session: Session,
    subscription: Subscription,
    payment: Payment,
    now: datetime,
) -> Payment:
    """Таймаут/5xx: решает GetState, новую Init не делаем."""
    if not payment.bank_payment_id:
        raise RetryLater("нет PaymentId для GetState")
    try:
        state = tbank.get_state(payment.bank_payment_id)
    except tbank.TbankError as exc:
        logger.warning("GetState failed for payment %s: %s", payment.id, exc)
        _schedule_reconcile(session, payment, now)
        session.commit()
        raise RetryLater("GetState недоступен") from exc

    status = str(state.get("Status", "")).upper()
    if status == "CONFIRMED":
        _succeed(session, subscription, payment, now)
        session.commit()
        return payment
    if status in {"REJECTED", "CANCELED", "CANCELLED"}:
        record_failure(session, subscription, payment, state, now)
        session.commit()
        return payment
    if status == "NEW" and subscription.rebill_id and payment.initiated_by_worker:
        try:
            response = tbank.charge(payment.bank_payment_id, subscription.rebill_id)
        except tbank.TbankError as exc:
            logger.warning("Charge after NEW failed for payment %s: %s", payment.id, exc)
            _schedule_reconcile(session, payment, now)
            session.commit()
            raise RetryLater("Charge после NEW недоступен") from exc
        return _charge_response(session, subscription, payment, response, now)

    _schedule_reconcile(session, payment, now)
    session.commit()
    raise RetryLater(f"статус {status or 'unknown'} не подтверждён")


def _charge_response(
    session: Session,
    subscription: Subscription,
    payment: Payment,
    response: dict,
    now: datetime,
) -> Payment:
    if response.get("Success"):
        _succeed(session, subscription, payment, now)
        session.commit()
        return payment
    record_failure(session, subscription, payment, response, now)
    session.commit()
    return payment


def _create_payment_row(session: Session, subscription: Subscription, now: datetime) -> Payment:
    tariff = subscription.tariff
    if tariff is None:
        raise RuntimeError("У подписки нет тарифа")
    receipt = receipts.build(tariff, subscription.client.email, subscription.client.phone)
    order_id = f"{subscription.client.tg_id}-{uuid4().hex[:10]}"
    payment = Payment(
        client_id=subscription.client_id,
        subscription_id=subscription.id,
        tariff_id=tariff.id,
        kind=PaymentKind.renewal,
        amount_kopecks=tariff.price_kopecks,
        order_id=order_id,
        status=PaymentStatus.created,
        receipt_json=receipt,
        initiated_by_worker=True,
    )
    session.add(payment)
    session.commit()
    return payment


def _init_and_charge(session: Session, subscription: Subscription, now: datetime) -> Payment:
    tariff = subscription.tariff
    if tariff is None:
        raise RuntimeError("У подписки нет тарифа")
    payment = _create_payment_row(session, subscription, now)

    try:
        response = tbank.init_payment(
            amount_kopecks=tariff.price_kopecks,
            order_id=payment.order_id,
            description=payments.payment_description(tariff),
            customer_key=str(subscription.client.tg_id),
            recurrent=False,
            receipt=payment.receipt_json,
            notification_url=settings.tbank_notification_url or None,
            extra={"OperationInitiatorType": "R"},
        )
    except tbank.TbankError as exc:
        # Сетевой сбой Init не является отказом карты: не трогаем attempts,
        # не шлём «карта отклонена», повторяем Init через 15 минут.
        logger.warning("Init failed for sub=%s: %s", subscription.id, exc)
        payment.status = PaymentStatus.rejected
        payment.error_code = "network"
        payment.error_message = str(exc)
        session.flush()
        jobs.enqueue(
            session,
            JobType.charge.value,
            now + INIT_RETRY_DELAY,
            {"subscription_id": subscription.id},
        )
        session.commit()
        return payment

    if not response.get("Success"):
        record_failure(session, subscription, payment, response, now)
        session.commit()
        return payment

    payment_id = response.get("PaymentId")
    if not payment_id:
        record_failure(
            session,
            subscription,
            payment,
            {"ErrorCode": "invalid_response", "Message": "Init без PaymentId"},
            now,
        )
        session.commit()
        return payment

    payment.bank_payment_id = str(payment_id)
    payment.status = PaymentStatus.pending
    session.commit()

    if not subscription.rebill_id:
        record_failure(
            session,
            subscription,
            payment,
            {"ErrorCode": "no_rebill", "Message": "Нет RebillId для списания"},
            now,
        )
        session.commit()
        return payment

    try:
        charge_response = tbank.charge(payment.bank_payment_id, subscription.rebill_id)
    except tbank.TbankError as exc:
        logger.warning("Charge failed for payment %s: %s", payment.id, exc)
        return _reconcile(session, subscription, payment, now)

    return _charge_response(session, subscription, payment, charge_response, now)


def charge_subscription(
    session: Session, subscription: Subscription, now: datetime | None = None
) -> Payment | None:
    """Списывает продление. None — списание не требуется (пауза/отмена/истекла)."""
    now = now or _now()
    if not subscription.auto_renew or not subscription.billing_enabled:
        return None
    if subscription.status in (SubscriptionStatus.expired, SubscriptionStatus.cancelled):
        return None

    pending = session.scalar(
        select(Payment)
        .where(
            Payment.subscription_id == subscription.id,
            Payment.kind == PaymentKind.renewal,
            Payment.status == PaymentStatus.pending,
            Payment.bank_payment_id.is_not(None),
            Payment.initiated_by_worker.is_(True),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )
    if pending is not None:
        return _reconcile(session, subscription, pending, now)

    return _init_and_charge(session, subscription, now)


def handle_job(session: Session, job: Job) -> None:
    payload = job.payload or {}
    subscription_id = payload.get("subscription_id")
    subscription = session.get(Subscription, subscription_id) if subscription_id else None
    if subscription is None:
        logger.warning("Задача %s без подписки: %s", job.id, payload)
        return

    job_type = _type_value(job.type)
    if job_type == JobType.expire.value:
        expire(session, subscription)
        return

    if job_type == JobType.kick.value:
        handle_kick(session, job)
        return

    if job_type == JobType.charge.value:
        try:
            charge_subscription(session, subscription)
        except RetryLater:
            logger.warning("Списание отложено до reconcile: sub=%s", subscription.id)
        reports.maybe_schedule(session, _now())
        return

    if job_type in {JobType.reminder_3d.value, JobType.reminder_1d.value}:
        if (
            not subscription.auto_renew
            or not subscription.billing_enabled
            or subscription.status != SubscriptionStatus.active
        ):
            return
        text = _reminder_text(job_type, subscription)
        if text:
            notify(subscription.client.tg_id, text)
        return

    logger.warning("Неизвестный тип задачи %s", job_type)
