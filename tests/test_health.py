from datetime import UTC, datetime, timedelta

import pytest
from app.db import SessionLocal
from app.models import ServiceHeartbeat
from app.services import health


@pytest.fixture
def beats():
    """Пишет отметки живости в общую тестовую базу и убирает их после теста."""

    def write(name: str, age_sec: int = 0) -> None:
        with SessionLocal() as session:
            health.beat(session, name, datetime.now(UTC) - timedelta(seconds=age_sec))
            session.commit()

    yield write
    with SessionLocal() as session:
        session.query(ServiceHeartbeat).delete()
        session.commit()


def test_healthz_ok(client, beats):
    beats(health.BOT)
    beats(health.BILLING)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["db"] is True
    assert r.json()["services"]["bot"]["ok"] is True
    assert r.json()["services"]["billing"]["ok"] is True


def test_healthz_fails_when_bot_is_stale(client, beats):
    beats(health.BOT, age_sec=600)
    beats(health.BILLING)
    r = client.get("/healthz")
    assert r.status_code == 503
    payload = r.json()
    assert payload["status"] == "error"
    assert payload["services"]["bot"]["ok"] is False
    assert payload["services"]["billing"]["ok"] is True


def test_healthz_fails_when_service_never_started(client, beats):
    beats(health.BILLING)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["services"]["bot"] == {"ok": False, "age_sec": None}


def test_healthz_ages_are_int_or_null(client, beats):
    beats(health.BOT)
    beats(health.BILLING)
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    for key in ("billing_age_sec", "bot_age_sec"):
        assert key in payload
        assert payload[key] is None or isinstance(payload[key], int)


def test_beat_updates_existing_row(beats):
    beats(health.BOT, age_sec=120)
    beats(health.BOT)
    with SessionLocal() as session:
        assert health.status(session)["bot"]["age_sec"] < 5
        assert session.query(ServiceHeartbeat).count() == 1
