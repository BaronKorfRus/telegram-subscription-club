from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.services import window
from app.services.window import Window

MSK = ZoneInfo("Europe/Moscow")


def test_window_opens_on_22nd_at_midnight():
    assert window.is_open(datetime(2026, 11, 22, 0, 0, tzinfo=MSK))
    assert not window.is_open(datetime(2026, 11, 21, 23, 59, tzinfo=MSK))


def test_window_closes_after_24th():
    assert window.is_open(datetime(2026, 11, 24, 23, 59, tzinfo=MSK))
    assert not window.is_open(datetime(2026, 11, 25, 0, 0, tzinfo=MSK))


def test_next_window_from_26th_is_next_month():
    assert window.next_window(datetime(2026, 10, 26, 12, 0, tzinfo=MSK)) == Window(
        date(2026, 11, 22), date(2026, 11, 24)
    )


def test_next_window_inside_window_is_current():
    assert window.next_window(datetime(2026, 11, 23, 12, 0, tzinfo=MSK)) == Window(
        date(2026, 11, 22), date(2026, 11, 24)
    )


def test_format_window_russian_month():
    assert window.format_window(Window(date(2026, 11, 22), date(2026, 11, 24))) == "22–24 ноября"


def test_window_for_uses_configured_days(monkeypatch):
    monkeypatch.setattr(window.settings, "payment_window_start_day", 5)
    monkeypatch.setattr(window.settings, "payment_window_end_day", 7)

    assert window.window_for(date(2026, 11, 20)) == Window(date(2026, 11, 5), date(2026, 11, 7))
    assert window.is_open(datetime(2026, 11, 6, 9, 0, tzinfo=MSK))
    assert not window.is_open(datetime(2026, 11, 8, 9, 0, tzinfo=MSK))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 12, 25, 0, 0, tzinfo=MSK), Window(date(2027, 1, 22), date(2027, 1, 24))),
        (datetime(2027, 1, 1, 12, 0, tzinfo=MSK), Window(date(2027, 1, 22), date(2027, 1, 24))),
    ],
)
def test_next_window_crosses_year(moment, expected):
    assert window.next_window(moment) == expected


def test_next_window_inside_after_window_is_next_month():
    assert window.next_window(datetime(2026, 11, 24, 23, 59, tzinfo=MSK)) == Window(
        date(2026, 11, 22), date(2026, 11, 24)
    )
    assert window.next_window(datetime(2026, 11, 25, 0, 1, tzinfo=MSK)) == Window(
        date(2026, 12, 22), date(2026, 12, 24)
    )
