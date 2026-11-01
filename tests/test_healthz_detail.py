import logging
from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Payment,
    PaymentKind,
    PaymentStatus,
)
from app.services import health
from app.web import routes_health
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

TOKEN = "test-healthz-token"


@pytest.fixture(autouse=True)
def clean_tables(session):
    _truncate(session)
    yield
    session.rollback()
    _truncate(session)


def _truncate(session):
    session.execute(text("TRUNCATE jobs RESTART IDENTITY"))
    session.execute(
        text("TRUNCATE payments, subscriptions, clients RESTART IDENTITY CASCADE")
    )
    session.execute(text("TRUNCATE service_heartbeats"))
    session.commit()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "healthz_token", TOKEN)
    return TOKEN


@pytest.fixture
def fresh_services(session):
    for name in health.SERVICES:
        health.beat(session, name)
    session.commit()


def _get(client, token_value=None):
    headers = {} if token_value is None else {"Authorization": f"Bearer {token_value}"}
    return client.get("/healthz", headers=headers)


def _now():
    return datetime.now(UTC)


def _job(session, *, run_at, status=JobStatus.pending, locked_until=None, job_type=JobType.charge):
    job = Job(
        type=job_type,
        run_at=run_at,
        payload={},
        status=status,
        locked_until=locked_until,
    )
    session.add(job)
    session.commit()
    return job


def _payment(session, *, status, age=timedelta(hours=2), worker=True, order_id="order-1"):
    client_row = Client(tg_id=980001, first_name="Тест")
    session.add(client_row)
    session.flush()
    payment = Payment(
        client_id=client_row.id,
        kind=PaymentKind.renewal,
        amount_kopecks=1000,
        order_id=order_id,
        status=status,
        created_at=_now() - age,
        initiated_by_worker=worker,
    )
    session.add(payment)
    session.commit()
    return payment


def test_healthz_without_token_returns_only_status(client, fresh_services):
    r = _get(client)

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_with_wrong_token_returns_only_status(client, token, fresh_services):
    r = _get(client, "wrong-token")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_short_answer_keeps_503_when_service_is_stale(client, token, session):
    health.beat(session, health.BOT, _now() - timedelta(seconds=600))
    health.beat(session, health.BILLING)
    session.commit()

    for headers in (
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
    ):
        r = client.get("/healthz", headers=headers)

        assert r.status_code == 503
        assert r.json() == {"status": "error"}


def test_healthz_with_token_returns_all_components(client, token, fresh_services):
    r = _get(client, token)

    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert payload["db"] is True
    assert payload["services"]["bot"]["ok"] is True
    assert payload["services"]["billing"]["ok"] is True
    assert "billing_age_sec" in payload
    assert "bot_age_sec" in payload
    assert payload["problems"] == []
    assert payload["components"]["db"] == {"status": "ok"}
    assert payload["components"]["bot"]["status"] == "ok"
    assert payload["components"]["bot"]["limit_sec"] == settings.health_stale_seconds
    assert payload["components"]["billing"]["status"] == "ok"
    assert payload["components"]["jobs"] == {
        "status": "ok",
        "overdue": 0,
        "failed_24h": 0,
        "next_charge_in_sec": None,
    }
    assert payload["components"]["payments"] == {
        "status": "ok",
        "rejected_24h": 0,
        "stuck": 0,
    }


def test_healthz_token_empty_always_returns_short(client, monkeypatch, fresh_services):
    monkeypatch.setattr(settings, "healthz_token", "")

    r = _get(client, TOKEN)

    assert r.json() == {"status": "ok"}


def test_overdue_pending_job_is_fail_with_http_200(client, token, fresh_services, session):
    _job(session, run_at=_now() - timedelta(minutes=16))

    r = _get(client, token)

    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "fail"
    assert payload["problems"] == ["jobs"]
    assert payload["components"]["jobs"]["status"] == "fail"
    assert payload["components"]["jobs"]["overdue"] == 1


def test_overdue_running_job_with_expired_lock(client, token, fresh_services, session):
    _job(
        session,
        run_at=_now() - timedelta(minutes=16),
        status=JobStatus.running,
        locked_until=_now() - timedelta(minutes=1),
    )

    payload = _get(client, token).json()

    assert payload["components"]["jobs"]["status"] == "fail"
    assert payload["components"]["jobs"]["overdue"] == 1


def test_running_job_with_live_lock_is_not_overdue(client, token, fresh_services, session):
    _job(
        session,
        run_at=_now() - timedelta(minutes=16),
        status=JobStatus.running,
        locked_until=_now() + timedelta(minutes=5),
    )

    payload = _get(client, token).json()

    assert payload["components"]["jobs"]["status"] == "ok"
    assert payload["components"]["jobs"]["overdue"] == 0
    assert payload["status"] == "ok"


def test_failed_job_last_day_is_warn(client, token, fresh_services, session):
    _job(
        session,
        run_at=_now() - timedelta(hours=2),
        status=JobStatus.failed,
        job_type=JobType.post_payment,
    )

    r = _get(client, token)

    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "warn"
    assert payload["problems"] == ["jobs"]
    assert payload["components"]["jobs"]["status"] == "warn"
    assert payload["components"]["jobs"]["failed_24h"] == 1


def test_failed_job_older_than_day_is_not_counted(client, token, fresh_services, session):
    _job(
        session,
        run_at=_now() - timedelta(hours=25),
        status=JobStatus.failed,
        job_type=JobType.post_payment,
    )

    payload = _get(client, token).json()

    assert payload["components"]["jobs"]["failed_24h"] == 0
    assert payload["components"]["jobs"]["status"] == "ok"


def test_next_charge_in_seconds(client, token, fresh_services, session):
    _job(session, run_at=_now() + timedelta(seconds=300))

    payload = _get(client, token).json()

    assert payload["components"]["jobs"]["overdue"] == 0
    assert 290 <= payload["components"]["jobs"]["next_charge_in_sec"] <= 300


def test_rejected_worker_payment_last_day_is_warn(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.rejected)

    payload = _get(client, token).json()

    assert payload["status"] == "warn"
    assert payload["problems"] == ["payments"]
    assert payload["components"]["payments"]["status"] == "warn"
    assert payload["components"]["payments"]["rejected_24h"] == 1
    assert payload["components"]["payments"]["stuck"] == 0


def test_manual_rejected_payment_is_not_counted(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.rejected, worker=False)

    payload = _get(client, token).json()

    assert payload["components"]["payments"]["rejected_24h"] == 0
    assert payload["components"]["payments"]["status"] == "ok"


def test_stuck_pending_payment_is_warn(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.pending)

    payload = _get(client, token).json()

    assert payload["components"]["payments"]["status"] == "warn"
    assert payload["components"]["payments"]["stuck"] == 1


def test_stale_bot_with_token_returns_503_and_full_report(client, token, session):
    health.beat(session, health.BOT, _now() - timedelta(seconds=600))
    health.beat(session, health.BILLING)
    session.commit()

    r = _get(client, token)

    assert r.status_code == 503
    payload = r.json()
    assert payload["status"] == "fail"
    assert payload["components"]["bot"]["status"] == "fail"
    assert payload["components"]["db"] == {"status": "ok"}


def test_failed_check_is_fail_and_others_still_count(
    client, token, fresh_services, monkeypatch, caplog
):
    def boom(session):
        raise RuntimeError("не удалось посчитать задачи")

    monkeypatch.setattr(routes_health, "_jobs_component", boom)

    with caplog.at_level(logging.ERROR):
        r = _get(client, token)

    assert r.status_code == 200
    payload = r.json()
    assert payload["components"]["jobs"] == {"status": "fail"}
    assert payload["components"]["payments"]["status"] == "ok"
    assert payload["components"]["db"] == {"status": "ok"}
    assert payload["status"] == "fail"
    assert payload["problems"] == ["jobs"]
    assert "jobs" in caplog.text


def test_services_check_failure_keeps_db_ok(client, token, monkeypatch, caplog):
    def boom(session):
        raise SQLAlchemyError("отметки живости недоступны")

    monkeypatch.setattr(routes_health.health_service, "status", boom)

    with caplog.at_level(logging.ERROR):
        r = _get(client, token)

    assert r.status_code == 503
    payload = r.json()
    assert payload["db"] is True
    assert payload["components"]["db"] == {"status": "ok"}
    assert payload["components"]["bot"]["status"] == "fail"
    assert payload["components"]["billing"]["status"] == "fail"
    assert payload["components"]["jobs"]["status"] == "ok"
    assert payload["components"]["payments"]["status"] == "ok"
    assert "сервис" in caplog.text


def test_db_unavailable_marks_all_components_fail(client, token, monkeypatch):
    def boom(*args, **kwargs):
        raise SQLAlchemyError("база недоступна")

    monkeypatch.setattr(routes_health.engine, "connect", boom)

    r = _get(client, token)

    assert r.status_code == 503
    payload = r.json()
    assert payload["db"] is False
    assert payload["components"] == {
        name: {"status": "fail"} for name in routes_health.COMPONENTS
    }
    assert payload["problems"] == list(routes_health.COMPONENTS)


def test_pending_job_younger_than_15_minutes_is_not_overdue(
    client, token, fresh_services, session
):
    _job(session, run_at=_now() - timedelta(minutes=5))

    payload = _get(client, token).json()

    assert payload["components"]["jobs"]["overdue"] == 0
    assert payload["components"]["jobs"]["status"] == "ok"


def test_rejected_payment_older_than_day_is_not_counted(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.rejected, age=timedelta(hours=25))

    payload = _get(client, token).json()

    assert payload["components"]["payments"]["rejected_24h"] == 0
    assert payload["components"]["payments"]["status"] == "ok"


def test_pending_payment_younger_than_hour_is_not_stuck(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.pending, age=timedelta(minutes=30))

    payload = _get(client, token).json()

    assert payload["components"]["payments"]["stuck"] == 0
    assert payload["components"]["payments"]["status"] == "ok"


def test_created_payment_older_than_hour_is_stuck(client, token, fresh_services, session):
    _payment(session, status=PaymentStatus.created)

    payload = _get(client, token).json()

    assert payload["components"]["payments"]["stuck"] == 1
    assert payload["components"]["payments"]["status"] == "warn"


def test_problems_keep_component_order(client, token, fresh_services, session):
    _job(session, run_at=_now() - timedelta(minutes=16))
    _payment(session, status=PaymentStatus.rejected)

    payload = _get(client, token).json()

    assert payload["status"] == "fail"
    assert payload["problems"] == ["jobs", "payments"]
