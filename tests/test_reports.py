from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import reports
from app.config import settings
from app.models import (
    Admin,
    AdminStatus,
    Client,
    Job,
    JobStatus,
    JobType,
    NotificationError,
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=MOSCOW)
DAY = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def clean_report_tables(session):
    session.execute(
        text(
            "TRUNCATE notification_errors, messages, jobs, payments, "
            "subscriptions, clients, tariffs, admins RESTART IDENTITY CASCADE"
        )
    )
    session.commit()
    yield
    session.rollback()
    session.execute(
        text(
            "TRUNCATE notification_errors, messages, jobs, payments, "
            "subscriptions, clients, tariffs, admins RESTART IDENTITY CASCADE"
        )
    )
    session.commit()


def _subscription(
    session,
    tg_id: int,
    *,
    name: str = "Анна Иванова",
    period_end: date = date(2026, 11, 22),
    forced_end: date | None = None,
    tariff_code: str = "m1",
) -> Subscription:
    seed_tariffs(session)
    tariff = session.query(Tariff).filter_by(code=tariff_code).one()
    first_name, _, last_name = name.partition(" ")
    client = Client(tg_id=tg_id, first_name=first_name, last_name=last_name or None)
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=NOW,
        current_period_end=period_end,
        forced_period_end=forced_end,
        auto_renew=True,
        billing_enabled=True,
    )
    session.add(subscription)
    session.flush()
    return subscription


def _payment(
    session,
    subscription: Subscription,
    *,
    amount: int | None = None,
    status: PaymentStatus = PaymentStatus.confirmed,
    index: int = 1,
    error_code: str | None = None,
) -> Payment:
    payment = Payment(
        client_id=subscription.client_id,
        subscription_id=subscription.id,
        tariff_id=subscription.tariff_id,
        kind=PaymentKind.renewal,
        amount_kopecks=subscription.tariff.price_kopecks if amount is None else amount,
        order_id=f"order-report-{index}",
        status=status,
        confirmed_at=NOW if status == PaymentStatus.confirmed else None,
        created_at=NOW,
        error_code=error_code,
    )
    session.add(payment)
    session.flush()
    return payment


def test_daily_summary_text(session):
    paid = _subscription(session, 990001)
    failed = _subscription(
        session,
        990002,
        name="Мария Петрова",
        period_end=date(2027, 1, 22),
        tariff_code="m3",
    )
    _payment(session, paid, index=1)
    _payment(session, failed, status=PaymentStatus.rejected, index=2, error_code="3")
    from app.services import jobs

    jobs.enqueue(
        session,
        JobType.charge.value,
        NOW + timedelta(hours=2),
        {"subscription_id": failed.id},
    )
    session.add(
        NotificationError(
            tg_id=990001,
            text="failed notification",
            error="Forbidden",
            created_at=NOW,
        )
    )
    session.flush()

    summary = reports.build_daily_summary(session, DAY)

    total = f"{paid.tariff.price_kopecks // 100:,}".replace(",", " ")
    assert "Сводка за 24.10.2026" in summary
    assert f"Списано: 1, сумма {total} ₽." in summary
    assert "Не прошло: 1." in summary
    assert "- Мария Петрова — код 3" in summary
    assert "Повторные попытки: 1, ближайшая 24.10.2026 14:00." in summary
    assert "Следующая плановая дата: 22.11.2026 — 1, 22.01.2027 — 1." in summary
    assert "Ошибки доставки сообщений: 1." in summary


def _add_confirmed(session, subscription: Subscription, count: int) -> None:
    for index in range(1, count + 1):
        _payment(session, subscription, index=index)


def test_maybe_schedule_small_batch_in_ten_minutes(session):
    subscription = _subscription(session, 990011)
    _add_confirmed(session, subscription, 9)

    job = reports.maybe_schedule(session, NOW)

    assert job is not None
    assert job.type == JobType.daily_report
    assert job.status == JobStatus.pending
    assert job.run_at == NOW + timedelta(minutes=10)
    assert job.payload == {"date": "2026-10-24"}


def test_maybe_schedule_bulk_next_morning(session):
    subscription = _subscription(session, 990012)
    _add_confirmed(session, subscription, 10)

    job = reports.maybe_schedule(session, NOW)

    assert job is not None
    assert job.run_at == datetime(2026, 10, 25, 9, 0, tzinfo=MOSCOW)


def test_maybe_schedule_is_idempotent(session):
    subscription = _subscription(session, 990013)
    _add_confirmed(session, subscription, 1)

    first = reports.maybe_schedule(session, NOW)
    second = reports.maybe_schedule(session, NOW)

    assert first is not None
    assert second is None
    assert session.query(Job).filter_by(type=JobType.daily_report).count() == 1


def test_send_summary_goes_to_owner_and_active_admin(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_tg_id", 990099)
    session.add(Admin(tg_id=990099, name="Владелец", is_owner=True, status=AdminStatus.active))
    session.add(Admin(tg_id=990100, name="Админ", status=AdminStatus.active))
    session.commit()
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        reports.telegram,
        "call",
        lambda method, params: calls.append((params["chat_id"], params["text"])),
    )

    sent = reports.send_summary(session, DAY, NOW)

    assert sent == 2
    assert {chat_id for chat_id, _ in calls} == {990099, 990100}
    assert all("Сводка за 24.10.2026" in text for _, text in calls)


def test_one_off_send_does_not_create_job(session, monkeypatch):
    monkeypatch.setattr(reports, "send_summary", lambda session, day, now=None: 1)

    assert reports.main(["send", "--date", "2026-10-24"]) == 0

    assert session.query(Job).filter_by(type=JobType.daily_report).count() == 0


def test_maybe_schedule_moves_pending_job_when_threshold_reached(session):
    subscription = _subscription(session, 990014)
    _add_confirmed(session, subscription, 9)

    first = reports.maybe_schedule(session, NOW)
    assert first is not None
    assert first.run_at == NOW + timedelta(minutes=10)

    _payment(session, subscription, index=10)
    reports.maybe_schedule(session, NOW)

    jobs = session.query(Job).filter_by(type=JobType.daily_report).all()
    assert len(jobs) == 1
    assert jobs[0].run_at == datetime(2026, 10, 25, 9, 0, tzinfo=MOSCOW)


def test_summary_never_goes_to_clients(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_tg_id", 990099)
    session.add(Admin(tg_id=990099, name="Владелец", is_owner=True, status=AdminStatus.active))
    session.add(Admin(tg_id=990100, name="Админ", status=AdminStatus.active))
    _subscription(session, 990101, name="Клиентка Без Прав")
    session.commit()
    calls: list[int] = []
    monkeypatch.setattr(
        reports.telegram,
        "call",
        lambda method, params: calls.append(params["chat_id"]),
    )

    reports.send_summary(session, DAY, NOW)

    assert 990101 not in calls
    assert set(calls) == {990099, 990100}
