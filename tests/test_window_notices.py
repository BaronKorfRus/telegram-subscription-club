from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import settings
from app.models import (
    Client,
    Job,
    JobType,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services import billing, window_notices
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
OWNER_TG_ID = 970099


@pytest.fixture(autouse=True)
def clean_tables(session):
    session.execute(
        text("TRUNCATE jobs, payments, subscriptions, clients, tariffs RESTART IDENTITY CASCADE")
    )
    session.commit()
    yield
    session.rollback()


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(settings, "owner_tg_id", OWNER_TG_ID)
    return OWNER_TG_ID


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        billing, "notify", lambda tg_id, text, reply_markup=None: sent.append((tg_id, text))
    )
    monkeypatch.setattr(
        window_notices, "notify", lambda tg_id, text, reply_markup=None: sent.append((tg_id, text))
    )
    return sent


def _client(session, tg_id, first_name="Анна", status=None, period_end=None):
    seed_tariffs(session)
    client = Client(tg_id=tg_id, first_name=first_name, email="a@example.com")
    session.add(client)
    session.flush()
    if status is not None:
        tariff = session.query(Tariff).filter_by(code="m1").one()
        session.add(
            Subscription(
                client=client,
                tariff=tariff,
                status=status,
                started_at=datetime(2026, 10, 22, 9, 0, tzinfo=MOSCOW),
                current_period_end=period_end or date(2026, 12, 22),
            )
        )
        session.flush()
    return client


def test_schedule_creates_two_jobs_and_is_idempotent(session):
    now = datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW)

    first = window_notices.maybe_schedule(session, now)
    second = window_notices.maybe_schedule(session, now)

    assert len(first) == 2
    assert second == []
    jobs = {job.type: job for job in session.query(Job).all()}
    assert set(jobs) == {JobType.window_soon, JobType.window_open}
    assert jobs[JobType.window_soon].run_at == datetime(2026, 11, 19, 12, 0, tzinfo=MOSCOW)
    assert jobs[JobType.window_open].run_at == datetime(2026, 11, 22, 12, 0, tzinfo=MOSCOW)
    assert jobs[JobType.window_soon].payload == {"date": "2026-11-19"}
    assert jobs[JobType.window_open].payload == {"date": "2026-11-22"}


def test_schedule_before_the_window_targets_the_next_window(session):
    now = datetime(2026, 11, 25, 9, 0, tzinfo=MOSCOW)

    window_notices.maybe_schedule(session, now)

    jobs = {job.type: job for job in session.query(Job).all()}
    assert jobs[JobType.window_soon].run_at == datetime(2026, 12, 19, 12, 0, tzinfo=MOSCOW)
    assert jobs[JobType.window_open].run_at == datetime(2026, 12, 22, 12, 0, tzinfo=MOSCOW)


def test_recipients_exclude_active_and_owner(session):
    _client(session, 970001, status=SubscriptionStatus.active)
    _client(session, 970002, status=SubscriptionStatus.past_due)
    _client(session, 970003, status=SubscriptionStatus.expired)
    _client(session, 970004)  # без подписки
    _client(session, OWNER_TG_ID)  # владелец — исключён

    tg_ids = {client.tg_id for client in window_notices.recipients(session)}

    assert tg_ids == {970003, 970004}


def test_handle_soon_sends_once_per_day(session, notifications):
    _client(session, 970001)
    _client(session, 970002)
    now = datetime(2026, 11, 19, 12, 0, tzinfo=MOSCOW)
    jobs_list = window_notices.maybe_schedule(session, now)
    soon = next(job for job in jobs_list if job.type == JobType.window_soon)

    sent = window_notices.handle_soon(session, soon)
    assert sent == 2
    assert {tg_id for tg_id, _text in notifications} == {970001, 970002}

    # Второй запуск в тот же день — без дублей (задача помечена done по payload.date).
    notifications.clear()
    again = window_notices.handle_soon(session, soon)
    assert again == 0
    assert notifications == []


def test_handle_open_sends_tariff_button(session, monkeypatch):
    _client(session, 970001)
    markup_seen = []
    sent = []

    def fake_notify(tg_id, text, reply_markup=None):
        sent.append((tg_id, text))
        markup_seen.append(reply_markup)

    monkeypatch.setattr(window_notices, "notify", fake_notify)
    now = datetime(2026, 11, 22, 12, 0, tzinfo=MOSCOW)
    jobs_list = window_notices.maybe_schedule(session, now)
    opened = next(job for job in jobs_list if job.type == JobType.window_open)

    count = window_notices.handle_open(session, opened)

    assert count == 1
    assert sent[0][0] == 970001
    assert markup_seen[0] is not None


def test_texts_use_the_month_of_the_notice(session, notifications, monkeypatch):
    """В декабре напоминание говорит «декабря», а не «ноября»."""
    _client(session, 970001)
    now = datetime(2026, 12, 15, 12, 0, tzinfo=MOSCOW)
    jobs_list = window_notices.maybe_schedule(session, now)
    soon = next(job for job in jobs_list if job.type == JobType.window_soon)
    window_notices.handle_soon(session, soon)
    assert "декабря" in notifications[0][1]
    assert "ноября" not in notifications[0][1]


def test_handle_open_skips_clients_who_bought_in_the_window(session, notifications):
    # Клиент оплатил 22-го: к моменту рассылки 12:00 у него действующий доступ.
    _client(session, 970001, status=SubscriptionStatus.active)
    _client(session, 970002, status=SubscriptionStatus.expired)
    now = datetime(2026, 11, 22, 12, 0, tzinfo=MOSCOW)
    jobs_list = window_notices.maybe_schedule(session, now)
    opened = next(job for job in jobs_list if job.type == JobType.window_open)

    window_notices.handle_open(session, opened)

    assert {tg_id for tg_id, _text in notifications} == {970002}


def test_handlers_registered_in_worker():
    from app import billing_worker

    assert billing_worker.HANDLERS["window_soon"] is not None
    assert billing_worker.HANDLERS["window_open"] is not None


def test_texts_are_required_keys():
    from app.content import REQUIRED_KEYS

    for key in ("window_soon", "window_open"):
        assert key in REQUIRED_KEYS


def test_worker_schedules_notices_each_cycle(session, monkeypatch):
    from app import billing_worker

    called = []
    monkeypatch.setattr(
        billing_worker.window_notices,
        "maybe_schedule",
        lambda session, now=None: called.append(now),
    )
    monkeypatch.setattr(billing_worker, "_now", lambda: datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW))

    billing_worker.run_once(now=datetime(2026, 11, 1, 9, 0, tzinfo=MOSCOW))

    assert called
