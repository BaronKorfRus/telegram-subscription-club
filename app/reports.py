"""Сводка владельцу после ежедневных списаний."""

import argparse
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import telegram
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Job,
    JobStatus,
    JobType,
    NotificationError,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
)
from app.services import jobs
from app.services import messages as messages_service

logger = logging.getLogger(__name__)
UPCOMING_DATES_LIMIT = 2


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _now() -> datetime:
    return datetime.now(_tz())


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=_tz())
    return start, start + timedelta(days=1)


def _format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _client_name(payment: Payment) -> str:
    client = payment.client
    if client is None:
        return str(payment.client_id)
    name = " ".join(filter(None, [client.first_name, client.last_name])).strip()
    return name or client.username or str(client.tg_id)


def _rubles(kopecks: int) -> str:
    return f"{kopecks // 100:,}".replace(",", " ")


def _confirmed_payments(session: Session, day: date) -> list[Payment]:
    start, end = _bounds(day)
    return list(
        session.scalars(
            select(Payment).where(
                Payment.kind == PaymentKind.renewal,
                Payment.status == PaymentStatus.confirmed,
                Payment.confirmed_at >= start,
                Payment.confirmed_at < end,
            )
        )
    )


def _failed_payments(session: Session, day: date) -> list[Payment]:
    start, end = _bounds(day)
    return list(
        session.scalars(
            select(Payment).where(
                Payment.kind == PaymentKind.renewal,
                Payment.status == PaymentStatus.rejected,
                Payment.created_at >= start,
                Payment.created_at < end,
            )
        )
    )


def _retry_jobs(session: Session, failed: list[Payment]) -> list[Job]:
    subscription_ids = {payment.subscription_id for payment in failed if payment.subscription_id}
    if not subscription_ids:
        return []
    return list(
        session.scalars(
            select(Job)
            .where(
                Job.type == JobType.charge.value,
                Job.status == JobStatus.pending,
                Job.payload["subscription_id"].as_integer().in_(subscription_ids),
            )
            .order_by(Job.run_at)
        )
    )


def _plan_end():
    """Дата окончания подписки: forced_period_end, иначе current_period_end."""
    return func.coalesce(Subscription.forced_period_end, Subscription.current_period_end)


def upcoming_dates(session: Session) -> list[date]:
    """Две ближайшие даты окончания действующих подписок — из базы, без констант."""
    rows = session.scalars(
        select(_plan_end())
        .where(Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)))
        .group_by(_plan_end())
        .order_by(_plan_end())
        .limit(UPCOMING_DATES_LIMIT)
    )
    return list(rows)


def _next_date_counts(session: Session, days: list[date]) -> dict[date, int]:
    if not days:
        return {}
    rows = session.execute(
        select(_plan_end(), func.count(Subscription.id))
        .where(
            Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)),
            _plan_end().in_(days),
        )
        .group_by(_plan_end())
    )
    return {row[0]: row[1] for row in rows}


def _delivery_errors(session: Session, day: date) -> int:
    start, end = _bounds(day)
    return int(
        session.scalar(
            select(func.count(NotificationError.id)).where(
                NotificationError.created_at >= start,
                NotificationError.created_at < end,
            )
        )
        or 0
    )


def build_daily_summary(session: Session, day: date) -> str:
    confirmed = _confirmed_payments(session, day)
    failed = _failed_payments(session, day)
    retries = _retry_jobs(session, failed)
    upcoming = upcoming_dates(session)
    date_counts = _next_date_counts(session, upcoming)
    delivery_errors = _delivery_errors(session, day)

    total = sum(payment.amount_kopecks for payment in confirmed)
    lines = [
        f"Сводка за {_format_date(day)}",
        "",
        f"Списано: {len(confirmed)}, сумма {_rubles(total)} ₽.",
        f"Не прошло: {len(failed)}.",
    ]
    for payment in failed:
        code = payment.error_code or "—"
        lines.append(f"- {_client_name(payment)} — код {code}")

    if retries:
        next_retry = retries[0].run_at.astimezone(_tz())
        lines.append(
            f"Повторные попытки: {len(retries)}, ближайшая {next_retry.strftime('%d.%m.%Y %H:%M')}."
        )
    else:
        lines.append("Повторных попыток нет.")

    if upcoming:
        plan = ", ".join(
            f"{_format_date(end)} — {date_counts.get(end, 0)}" for end in upcoming
        )
        lines.append(f"Следующая плановая дата: {plan}.")
    if delivery_errors:
        lines.append(f"Ошибки доставки сообщений: {delivery_errors}.")
    return "\n".join(lines)


def send_summary(session: Session, day: date, now: datetime | None = None) -> int:
    text = build_daily_summary(session, day)
    recipients: list[int] = []
    if settings.owner_tg_id is not None:
        recipients.append(settings.owner_tg_id)
    for admin in messages_service.active_admins(session):
        if admin.tg_id not in recipients:
            recipients.append(admin.tg_id)
    if not recipients:
        raise RuntimeError("Нет получателей сводки (OWNER_TG_ID/админы)")

    sent = 0
    for tg_id in recipients:
        try:
            telegram.call("sendMessage", {"chat_id": tg_id, "text": text})
        except telegram.TelegramError:
            logger.exception("Не удалось отправить сводку tg_id=%s", tg_id)
        else:
            sent += 1
    return sent


def maybe_schedule(session: Session, now: datetime | None = None) -> Job | None:
    now = now or _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    now = now.astimezone(_tz())
    day = now.date()
    confirmed_count = len(_confirmed_payments(session, day))
    if confirmed_count >= settings.summary_bulk_threshold:
        run_at = datetime.combine(day + timedelta(days=1), time(9), tzinfo=_tz())
    else:
        run_at = now + timedelta(minutes=10)

    existing = session.scalar(
        select(Job)
        .where(
            Job.type == JobType.daily_report.value,
            Job.status.in_((JobStatus.pending, JobStatus.running, JobStatus.done)),
            Job.payload["date"].as_string() == day.isoformat(),
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    if existing is not None:
        existing_run_at = existing.run_at
        if existing_run_at.tzinfo is None:
            existing_run_at = existing_run_at.replace(tzinfo=_tz())
        if existing.status == JobStatus.pending and run_at > existing_run_at:
            existing.run_at = run_at
            session.flush()
        return None

    job = jobs.enqueue(
        session,
        JobType.daily_report.value,
        run_at,
        {"date": day.isoformat()},
    )
    session.flush()
    return job


def handle_job(session: Session, job: Job) -> None:
    raw = (job.payload or {}).get("date")
    day = date.fromisoformat(raw) if raw else _now().date()
    send_summary(session, day, _now())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сводка оплат")
    subparsers = parser.add_subparsers(dest="command", required=True)
    send_parser = subparsers.add_parser("send", help="отправить сводку за дату")
    send_parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args(argv)

    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print("Ошибка: дата должна быть YYYY-MM-DD")
        return 1

    with SessionLocal() as session:
        try:
            sent = send_summary(session, day, _now())
        except Exception as exc:
            print(f"Ошибка отправки сводки: {exc}")
            return 1
    print(f"Сводка отправлена получателям: {sent}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
