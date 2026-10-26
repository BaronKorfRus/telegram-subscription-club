"""Окно оплаты: 22–24 число каждого месяца по Москве.

Единственный источник правды про окно. Дни окна берутся из настроек
(`PAYMENT_WINDOW_START_DAY` / `PAYMENT_WINDOW_END_DAY`).
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings

MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@dataclass(frozen=True, order=True)
class Window:
    """Границы окна оплаты: первое и последнее число включительно."""

    start: date
    end: date


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _to_local(moment: datetime) -> datetime:
    """Naive-время считается уже московским, aware — приводится к Москве."""
    tz = _tz()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _window_days() -> tuple[int, int]:
    return settings.payment_window_start_day, settings.payment_window_end_day


def _clamp_day(year: int, month: int, day: int) -> int:
    """День внутри месяца: окно не должно вылезать за 28 февраля."""
    return min(day, monthrange(year, month)[1])


def window_for(day: date) -> Window:
    """Окно месяца, в котором лежит дата."""
    start_day, end_day = _window_days()
    return Window(
        date(day.year, day.month, _clamp_day(day.year, day.month, start_day)),
        date(day.year, day.month, _clamp_day(day.year, day.month, end_day)),
    )


def is_open(moment: datetime) -> bool:
    """True для дней окна включительно по часовому поясу клуба."""
    local = _to_local(moment)
    current = window_for(local.date())
    return current.start <= local.date() <= current.end


def next_window(moment: datetime) -> Window:
    """Текущее окно, если оно открыто; иначе окно следующего месяца."""
    local = _to_local(moment)
    current = window_for(local.date())
    if current.start <= local.date() <= current.end:
        return current
    if local.date() < current.start:
        return current
    year = local.year + (1 if local.month == 12 else 0)
    month = 1 if local.month == 12 else local.month + 1
    return window_for(date(year, month, 1))


def format_window(window: Window) -> str:
    """«22–24 ноября»; для окна в один день — «22 ноября»."""
    month = MONTHS_GENITIVE[window.start.month - 1]
    if window.start == window.end:
        return f"{window.start.day} {month}"
    if window.start.month == window.end.month:
        return f"{window.start.day}–{window.end.day} {month}"
    end_month = MONTHS_GENITIVE[window.end.month - 1]
    return f"{window.start.day} {month} – {window.end.day} {end_month}"
