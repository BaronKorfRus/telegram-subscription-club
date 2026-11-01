import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import billing_worker
from app.db import SessionLocal
from app.models import Job, JobStatus, JobType
from app.services.jobs import claim, done, enqueue, fail
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)


@pytest.fixture(autouse=True)
def clean_jobs(session):
    _truncate(session)
    yield
    session.rollback()
    _truncate(session)


def _truncate(session):
    session.execute(text("TRUNCATE jobs RESTART IDENTITY"))
    session.commit()


def test_enqueue_creates_pending_job(session):
    job = enqueue(session, "charge", NOW, {"subscription_id": 1})

    assert job.id is not None
    assert job.type == JobType.charge
    assert job.status == JobStatus.pending
    assert job.attempts == 0
    assert job.locked_until is None
    assert job.payload == {"subscription_id": 1}


def test_claim_only_due_pending_jobs(session):
    enqueue(session, "charge", NOW + timedelta(hours=1), {"n": 1})

    assert claim(session, NOW) == []


def test_claim_survives_restart(session):
    job = enqueue(session, "charge", NOW - timedelta(seconds=1), {"subscription_id": 1})

    first = claim(session, NOW)

    assert len(first) == 1
    assert first[0].id == job.id
    assert first[0].status == JobStatus.running
    assert first[0].locked_until == NOW + timedelta(minutes=5)
    assert first[0].attempts == 1

    first[0].locked_until = None  # имитация падения воркера
    session.commit()

    second = claim(session, NOW + timedelta(minutes=1))

    assert [j.id for j in second] == [job.id]
    assert second[0].attempts == 2


def test_two_workers_do_not_claim_the_same_job(session):
    first_job = enqueue(session, "charge", NOW - timedelta(seconds=1), {"n": 1})
    second_job = enqueue(session, "charge", NOW - timedelta(seconds=1), {"n": 2})
    session.commit()

    other = SessionLocal()
    try:
        first = claim(session, NOW, limit=1)
        second = claim(other, NOW, limit=1)

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].id != second[0].id
        assert {first[0].id, second[0].id} == {first_job.id, second_job.id}
    finally:
        other.rollback()
        other.close()


def test_done_marks_job_done_and_releases_lock(session):
    enqueue(session, "charge", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]
    job.last_error = "old error"

    done(session, job)

    assert job.status == JobStatus.done
    assert job.locked_until is None
    assert job.last_error is None


def test_fail_retries_with_backoff(session):
    enqueue(session, "charge", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]

    fail(session, job, "bank timeout", now=NOW)

    assert job.status == JobStatus.pending
    assert job.locked_until is None
    assert job.last_error == "bank timeout"
    assert job.run_at == NOW + timedelta(minutes=1)


def test_worker_processes_known_handler(session, monkeypatch):
    processed = []
    monkeypatch.setitem(billing_worker.HANDLERS, "charge", lambda s, j: processed.append(j.id))
    enqueue(session, "charge", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]

    billing_worker.process_claimed(session, [job])

    assert processed == [job.id]
    assert job.status == JobStatus.done


def test_worker_completes_unknown_type_without_handler(session, caplog, monkeypatch):
    monkeypatch.delitem(billing_worker.HANDLERS, "reconcile")
    enqueue(session, "reconcile", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]

    with caplog.at_level(logging.WARNING, logger="app.billing_worker"):
        billing_worker.process_claimed(session, [job])

    assert job.status == JobStatus.done
    assert "No handler" in caplog.text


def test_worker_retries_handler_error(session, monkeypatch):
    def boom(session, job):
        raise RuntimeError("boom")

    monkeypatch.setitem(billing_worker.HANDLERS, "kick", boom)
    enqueue(session, "kick", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]
    session.commit()

    billing_worker.process_claimed(session, [job])

    assert job.status == JobStatus.pending
    assert job.locked_until is None
    assert "boom" in job.last_error


def test_run_once_claims_and_completes_job(session, monkeypatch):
    monkeypatch.setitem(billing_worker.HANDLERS, "charge", lambda s, j: None)
    job = enqueue(session, "charge", NOW - timedelta(seconds=1), {})
    session.commit()

    assert billing_worker.run_once(now=NOW) == 1

    session.expire_all()
    stored = session.get(Job, job.id)
    assert stored.status == JobStatus.done
    assert stored.locked_until is None


def test_fail_backoff_and_terminal_after_five(session, caplog):
    enqueue(session, "charge", NOW - timedelta(seconds=1), {})
    job = claim(session, NOW)[0]

    for attempts, minutes in ((1, 1), (2, 5), (3, 15), (4, 60)):
        job.attempts = attempts
        job.status = JobStatus.running
        fail(session, job, "boom", now=NOW)
        assert job.status == JobStatus.pending
        assert job.run_at == NOW + timedelta(minutes=minutes)

    job.attempts = 5
    job.status = JobStatus.running
    with caplog.at_level(logging.CRITICAL, logger="app.services.jobs"):
        fail(session, job, "boom", now=NOW)

    assert job.status == JobStatus.failed
    assert "окончательно упала" in caplog.text
