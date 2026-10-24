from datetime import date, datetime, timedelta
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


def _period_end(now: datetime, period_days: int) -> date:
    """Конец периода: дата фактического списания + календарные дни тарифа."""
    return _moscow_date(now) + timedelta(days=period_days)


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
        current_period_end=_period_end(now, tariff.period_days),
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
    """Продлевает от максимума текущего конца периода и даты списания."""
    if subscription.forced_period_end is not None:
        subscription.current_period_end = subscription.forced_period_end
        subscription.forced_period_end = None
    else:
        base = max(subscription.current_period_end, _moscow_date(now))
        subscription.current_period_end = base + timedelta(days=subscription.tariff.period_days)
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
