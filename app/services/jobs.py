import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, JobStatus, JobType

logger = logging.getLogger(__name__)

LOCK_TTL = timedelta(minutes=5)
RETRY_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
)
MAX_ATTEMPTS = 5


def enqueue(session: Session, type: str, run_at: datetime, payload: dict) -> Job:
    job = Job(
        type=JobType(type),
        run_at=run_at,
        payload=dict(payload),
        status=JobStatus.pending,
    )
    session.add(job)
    session.flush()
    return job


def claim(session: Session, now: datetime, limit: int = 20) -> list[Job]:
    """Забирает готовые задачи и блокирует их до now + 5 минут.

    Строки блокируются FOR UPDATE SKIP LOCKED, поэтому параллельный воркер их
    не увидит. Задача, чей воркер упал, снова доступна после истечения
    locked_until (или сразу, если locked_until = NULL).
    """
    if limit <= 0:
        return []

    stmt = (
        select(Job)
        .where(
            Job.status.in_((JobStatus.pending, JobStatus.running)),
            Job.run_at <= now,
            or_(Job.locked_until.is_(None), Job.locked_until <= now),
        )
        .order_by(Job.run_at, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(session.scalars(stmt))
    for job in claimed:
        job.status = JobStatus.running
        job.locked_until = now + LOCK_TTL
        job.attempts += 1
    session.flush()
    return claimed


def done(session: Session, job: Job) -> None:
    job.status = JobStatus.done
    job.locked_until = None
    job.last_error = None
    session.flush()


def fail(session: Session, job: Job, error: object, now: datetime | None = None) -> None:
    """Повторяет задачу с backoff; после 5 попыток — терминальный failed."""
    now = now or datetime.now(ZoneInfo(settings.tz))
    job.locked_until = None
    job.last_error = str(error)
    if job.attempts >= MAX_ATTEMPTS:
        job.status = JobStatus.failed
        logger.critical(
            "Задача %s (%s) окончательно упала после %s попыток: %s",
            job.id,
            job.type,
            job.attempts,
            job.last_error,
        )
    else:
        job.status = JobStatus.pending
        delay = RETRY_BACKOFF[min(max(job.attempts - 1, 0), len(RETRY_BACKOFF) - 1)]
        job.run_at = now + delay
        logger.warning(
            "Задача %s (%s) упала, попытка %s, повтор через %s",
            job.id,
            job.type,
            job.attempts,
            delay,
        )
    session.flush()
