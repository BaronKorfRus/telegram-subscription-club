from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.bot.handlers import join
from app.bot.handlers.join import handle_join
from app.models import Client, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs

MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 23)
TG_ID = 930001


@pytest.fixture
def approve_calls(monkeypatch):
    calls: list[int] = []

    def fake_approve(tg_id: int) -> bool:
        calls.append(tg_id)
        return True

    monkeypatch.setattr(join.access, "approve_join", fake_approve)
    return calls


def _subscription(session, status: SubscriptionStatus, period_end: date) -> Subscription:
    seed_tariffs(session)
    tariff = session.query(Tariff).filter_by(code="m1").one()
    client = Client(tg_id=TG_ID, phone="+79991234567")
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=status,
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=period_end,
    )
    session.add(subscription)
    session.flush()
    return subscription


@pytest.mark.parametrize(
    ("status", "period_end", "expected"),
    [
        (SubscriptionStatus.active, date(2026, 11, 22), True),
        (SubscriptionStatus.past_due, date(2026, 10, 1), True),
        (SubscriptionStatus.expired, date(2026, 9, 1), False),
        (SubscriptionStatus.cancelled, date(2026, 11, 22), True),
        (SubscriptionStatus.cancelled, date(2026, 10, 22), False),
    ],
)
def test_handle_join_access(session, approve_calls, status, period_end, expected):
    _subscription(session, status, period_end)

    assert handle_join(session, TG_ID, today=TODAY) is expected
    assert approve_calls == ([TG_ID] if expected else [])


def test_handle_join_without_subscription_is_denied(session, approve_calls):
    client = Client(tg_id=TG_ID, phone="+79991234567")
    session.add(client)
    session.flush()

    assert handle_join(session, TG_ID, today=TODAY) is False
    assert approve_calls == []


def test_handle_join_unknown_client_is_denied(session, approve_calls):
    assert handle_join(session, TG_ID, today=TODAY) is False
    assert approve_calls == []
