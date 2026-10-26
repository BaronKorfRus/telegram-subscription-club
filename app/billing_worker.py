import logging
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import reports
from app.config import settings
from app.db import SessionLocal
from app.models import Job, JobType
from app.services import billing, jobs, post_payment, reconcile, window_notices

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
HANDLERS: dict[str, Callable[[Session, Job], None]] = {
    "charge": billing.handle_job,
    "reminder_3d": billing.handle_job,
    "reminder_1d": billing.handle_job,
    "post_payment": post_payment.handle,
    "payment_failed": billing.handle_payment_failed,
    "daily_report": reports.handle_job,
    "kick": billing.handle_kick,
    "expire": billing.handle_job,
    "expire_check": billing.handle_expire_check,
    "window_soon": window_notices.handle_soon,
    "window_open": window_notices.handle_open,
    "reconcile": reconcile.handle_job,
}


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _type_value(job_type: JobType | str) -> str:
    return job_type.value if isinstance(job_type, JobType) else str(job_type)


def dispatch(session: Session, job: Job) -> None:
    handler = HANDLERS.get(_type_value(job.type))
    if handler is None:
        # Типы charge/reminder_* появятся в Task 13; неизвестные не должны
        # застревать в очереди.
        logger.warning("No handler for job type=%s id=%s; marking done", job.type, job.id)
        return
    handler(session, job)


def process_job(session: Session, job: Job) -> None:
    job_id = job.id
    job_type = job.type
    try:
        dispatch(session, job)
    except Exception as exc:
        logger.exception("Job failed: id=%s type=%s", job_id, job_type)
        # billing коммитит внутри; после ошибки нужна чистая транзакция.
        session.rollback()
        failed_job = session.get(Job, job_id)
        if failed_job is None:
            logger.error("Задача %s исчезла после rollback", job_id)
            return
        jobs.fail(session, failed_job, exc)
    else:
        jobs.done(session, job)


def process_claimed(session: Session, claimed: list[Job]) -> None:
    for job in claimed:
        process_job(session, job)
    session.flush()


def bootstrap(now: datetime | None = None) -> Job:
    """На старте гарантируем одну ближайшую задачу reconcile."""
    with SessionLocal() as session:
        job = reconcile.schedule_next(session, now)
        session.commit()
        return job


def run_once(now: datetime | None = None) -> int:
    """Один цикл: забрать задачи, обработать, зафиксировать результат."""
    moment = now or _now()
    with SessionLocal() as session:
        try:
            window_notices.maybe_schedule(session, moment)
            session.commit()
        except Exception:
            logger.exception("billing_worker: не удалось поставить напоминания окна")
            session.rollback()
        claimed = jobs.claim(session, moment)
        # Локи снимаются сразу: внешние вызовы не держат транзакцию.
        session.commit()
        for job in claimed:
            process_job(session, job)
            session.commit()
        return len(claimed)


def main() -> None:
    logger.info("billing_worker started, poll=%ss", POLL_SECONDS)
    try:
        bootstrap()
    except Exception:
        logger.exception("billing_worker: не удалось поставить reconcile")
    while True:
        try:
            processed = run_once()
        except Exception:
            logger.exception("billing_worker cycle failed")
        else:
            if processed:
                logger.info("billing_worker processed %s jobs", processed)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
