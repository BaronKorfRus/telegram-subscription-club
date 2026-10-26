"""Напоминания зарегистрированным, но не оплатившим: 19-го и 22-го в 12:00 МСК.

Идемпотентны по `payload["date"]`, как ежедневная сводка: повторный запуск в тот
же день не дублирует сообщения.
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import texts
from app.config import settings
from app.models import Client, Job, JobStatus, JobType, Subscription
from app.services import billing, jobs, subscriptions, window

logger = logging.getLogger(__name__)

SOON_HOUR = 12
OPEN_HOUR = 12


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _now() -> datetime:
    return datetime.now(_tz())


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_tz())
    return moment.astimezone(_tz())


def notify(tg_id: int, text: str, reply_markup: dict | None = None) -> None:
    """Обёртка над billing.notify (тесты подменяют её здесь)."""
    billing.notify(tg_id, text, reply_markup=reply_markup)


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=_tz())


def _tariff_button() -> dict:
    from app.bot.keyboards import tariff_keyboard

    return tariff_keyboard().model_dump(exclude_none=True)


def _existing(session: Session, job_type: str, day: date) -> bool:
    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.type == job_type,
                Job.status.in_((JobStatus.pending, JobStatus.running, JobStatus.done)),
                Job.payload["date"].as_string() == day.isoformat(),
            )
            .limit(1)
        )
        is not None
    )


def _next_notice_window(moment: datetime) -> window.Window:
    """Окно, для которого ставим напоминания: текущее, если его 22-е ещё впереди."""
    target = window.next_window(moment)
    if target.start < moment.date():
        # Окно уже открылось (или прошло): напоминания для него уже неактуальны.
        year = target.start.year + (1 if target.start.month == 12 else 0)
        month = 1 if target.start.month == 12 else target.start.month + 1
        target = window.window_for(date(year, month, 1))
    return target


def maybe_schedule(session: Session, now: datetime | None = None) -> list[Job]:
    """Ставит напоминания на 19-е и 22-е ближайшего окна; идемпотентно по дате."""
    moment = _aware(now or _now())
    target = _next_notice_window(moment)
    soon_day = target.start - timedelta(days=3)
    plan = (
        (JobType.window_soon.value, soon_day, _at(soon_day, SOON_HOUR)),
        (JobType.window_open.value, target.start, _at(target.start, OPEN_HOUR)),
    )

    created: list[Job] = []
    for job_type, day, run_at in plan:
        if run_at < moment:
            # Время напоминания уже прошло в этом цикле — не шлём задним числом.
            continue
        if _existing(session, job_type, day):
            continue
        created.append(
            jobs.enqueue(session, job_type, run_at, {"date": day.isoformat()})
        )
    session.flush()
    return created


def recipients(session: Session) -> list[Client]:
    """Клиенты без действующего доступа, кроме владельца."""
    today = _now().date()
    rows = session.scalars(select(Client).order_by(Client.id))
    result: list[Client] = []
    for client in rows:
        if settings.owner_tg_id is not None and client.tg_id == settings.owner_tg_id:
            continue
        if client.is_blocked:
            continue
        subscriptions_list = session.scalars(
            select(Subscription).where(Subscription.client_id == client.id)
        ).all()
        if any(
            subscriptions.has_access(subscription, today) for subscription in subscriptions_list
        ):
            continue
        result.append(client)
    return result


def _paywall_text(job_type: str) -> str:
    if job_type == JobType.window_soon.value:
        return texts.WINDOW_SOON
    return texts.WINDOW_OPEN


def _send(session: Session, job: Job, job_type: str) -> int:
    if job.status == JobStatus.done:
        # Повторный запуск в тот же день: задача уже отработала.
        return 0

    raw = (job.payload or {}).get("date")
    day = date.fromisoformat(str(raw)) if raw else _now().date()
    text = _paywall_text(job_type)
    markup = _tariff_button() if job_type == JobType.window_open.value else None

    sent = 0
    for client in recipients(session):
        notify(client.tg_id, text, markup)
        sent += 1
    job.status = JobStatus.done
    session.flush()
    logger.info("window_notices %s за %s: отправлено %s", job_type, day, sent)
    return sent


def handle_soon(session: Session, job: Job) -> int:
    return _send(session, job, JobType.window_soon.value)


def handle_open(session: Session, job: Job) -> int:
    return _send(session, job, JobType.window_open.value)
