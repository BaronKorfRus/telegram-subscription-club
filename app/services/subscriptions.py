from calendar import monthrange
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Client,
    Job,
    JobStatus,
    JobType,
    Payment,
    Subscription,
    SubscriptionStatus,
    Tariff,
)


def _timezone() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _moscow_date(moment: datetime) -> date:
    """Календарная дата по Москве; naive-время считается уже московским."""
    tz = _timezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz).date()


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_timezone())
    return moment


def _anchor_day() -> int:
    """День, на который всегда приходится конец периода."""
    return settings.payment_window_start_day


def add_months(day: date, months: int) -> date:
    """Та же дата через N месяцев с днём, приведённым к 22-му.

    День месяца — из настройки окна (`PAYMENT_WINDOW_START_DAY`), месяц и год —
    из `day` плюс `months`. Переход через декабрь и февраль — обычная
    календарная арифметика.
    """
    if months < 0:
        raise ValueError("months не может быть отрицательным")
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(_anchor_day(), monthrange(year, month)[1]))


def _period_end(now: datetime, months: int) -> date:
    """Конец периода: 22-е месяца оплаты плюс months календарных месяцев."""
    return add_months(_moscow_date(now), months)


def has_access(subscription: Subscription | None, today: date) -> bool:
    """Единое правило доступа: active/past_due — да, cancelled — до конца периода."""
    if subscription is None:
        return False
    if subscription.status in (SubscriptionStatus.active, SubscriptionStatus.past_due):
        return True
    if subscription.status == SubscriptionStatus.cancelled:
        return subscription.current_period_end >= today
    return False


def activate(
    session: Session,
    client: Client,
    tariff: Tariff,
    payment: Payment,
    now: datetime,
) -> Subscription:
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=_aware(now),
        current_period_end=_period_end(now, tariff.period_months),
        auto_renew=True,
        billing_enabled=True,
        failed_attempts=0,
    )
    payment.subscription = subscription
    session.add(subscription)
    session.flush()
    return subscription


def extend(
    session: Session,
    subscription: Subscription,
    payment: Payment,
    now: datetime,
) -> Subscription:
    """Продлевает от текущего окончания (или forced) на months месяцев."""
    if subscription.forced_period_end is not None:
        subscription.current_period_end = subscription.forced_period_end
        subscription.forced_period_end = None
    else:
        subscription.current_period_end = add_months(
            subscription.current_period_end, subscription.tariff.period_months
        )
    subscription.status = SubscriptionStatus.active
    subscription.failed_attempts = 0
    payment.subscription = subscription
    session.flush()
    return subscription


def cancel(session: Session, subscription: Subscription, now: datetime) -> Subscription:
    """Отключает автопродление; доступ остаётся до current_period_end."""
    subscription.auto_renew = False
    subscription.rebill_id = None
    subscription.status = SubscriptionStatus.cancelled
    # После отмены не нужны ни списание, ни напоминания о нём. kick остаётся.
    session.execute(
        delete(Job).where(
            Job.type.in_(
                (
                    JobType.charge.value,
                    JobType.reminder_3d.value,
                    JobType.reminder_1d.value,
                )
            ),
            Job.status == JobStatus.pending,
            Job.payload["subscription_id"].as_integer() == subscription.id,
        )
    )
    session.flush()
    return subscription


def set_billing_enabled(session: Session, subscription: Subscription, flag: bool) -> None:
    subscription.billing_enabled = flag
    session.flush()
