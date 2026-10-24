import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import telegram, texts
from app.config import settings
from app.models import (
    Job,
    JobStatus,
    JobType,
    Message,
    MessageDirection,
    MessageKind,
    Payment,
    PaymentStatus,
)
from app.services import access, jobs
from app.services import messages as messages_service

logger = logging.getLogger(__name__)

RETRY_DELAY = timedelta(minutes=5)
RETRY_WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _success_markup() -> dict | None:
    link = settings.channel_invite_link
    if not link:
        return None
    return {
        "inline_keyboard": [
            [{"text": texts.PAYMENT_SUCCESS_BUTTON, "url": link}],
        ]
    }


def notify(tg_id: int, text: str, reply_markup: dict | None = None) -> bool:
    params: dict = {"chat_id": tg_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        telegram.call("sendMessage", params)
        return True
    except telegram.TelegramError as exc:
        logger.exception("Не удалось отправить сообщение клиенту tg_id=%s", tg_id)
        messages_service.record_delivery_error(tg_id, text, exc)
        return False


def enqueue(session: Session, payment: Payment, now: datetime | None = None) -> Job | None:
    """Ставит post_payment, если нет активной задачи и всё уже не выполнено."""
    if payment.post_payment_done:
        return None
    existing = session.scalar(
        select(Job).where(
            Job.type == JobType.post_payment,
            Job.payload["payment_id"].as_integer() == payment.id,
            Job.status.in_((JobStatus.pending, JobStatus.running)),
        )
    )
    if existing is not None:
        return existing
    now = now or _now()
    return jobs.enqueue(
        session,
        JobType.post_payment.value,
        now,
        {"payment_id": payment.id, "first_attempt_at": now.isoformat()},
    )


def _schedule_retry(session: Session, payment: Payment, job: Job, now: datetime) -> Job | None:
    first_raw = (job.payload or {}).get("first_attempt_at")
    try:
        first_attempt = datetime.fromisoformat(first_raw) if first_raw else now
    except ValueError:
        first_attempt = now
    if first_attempt.tzinfo is None:
        first_attempt = first_attempt.replace(tzinfo=ZoneInfo(settings.tz))
    if now - first_attempt >= RETRY_WINDOW:
        logger.error("post_payment: истекли 24 часа повторов payment=%s", payment.id)
        return None

    existing = session.scalar(
        select(Job).where(
            Job.type == JobType.post_payment,
            Job.payload["payment_id"].as_integer() == payment.id,
            Job.status.in_((JobStatus.pending, JobStatus.running)),
            Job.id != job.id,
        )
    )
    if existing is not None:
        return existing
    return jobs.enqueue(
        session,
        JobType.post_payment.value,
        now + RETRY_DELAY,
        {"payment_id": payment.id, "first_attempt_at": first_attempt.isoformat()},
    )


def handle(session: Session, job: Job) -> None:
    payment_id = (job.payload or {}).get("payment_id")
    if not payment_id:
        logger.warning("post_payment без payment_id: job=%s", job.id)
        return

    payment = session.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
    if payment is None:
        logger.warning("post_payment: платёж %s не найден", payment_id)
        return
    if payment.post_payment_done:
        return
    if payment.status != PaymentStatus.confirmed or payment.subscription is None:
        logger.warning("post_payment: платёж %s не подтверждён/без подписки", payment_id)
        return

    subscription = payment.subscription
    tg_id = payment.client.tg_id
    now = _now()
    # grant синхронный; воркер сам синхронный, to_thread не нужен.
    try:
        access.grant(tg_id)
    except access.AccessError:
        logger.exception("post_payment: grant не удался payment=%s", payment.id)
        _schedule_retry(session, payment, job, now)
        return

    link = settings.channel_invite_link or ""
    text = texts.PAYMENT_SUCCESS.format(
        date=subscription.current_period_end.strftime("%d.%m.%Y"), link=link
    )
    if not notify(tg_id, text, _success_markup()):
        _schedule_retry(session, payment, job, now)
        return

    session.add(
        Message(
            client_id=payment.client_id,
            direction=MessageDirection.outbound,
            kind=MessageKind.bot,
            text=text,
            is_in_flow=False,
            created_at=now,
        )
    )
    payment.post_payment_done = True
    session.flush()
    logger.info("post_payment выполнен: payment=%s tg_id=%s", payment.id, tg_id)
