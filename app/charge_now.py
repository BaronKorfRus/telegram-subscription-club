"""Безопасно ставит задачу charge на now: списание выполнит billing-воркер.

Использование: python -m app.charge_now --subscription <id>
"""

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Job, Subscription, SubscriptionStatus
from app.services import jobs


class ChargeNowError(RuntimeError):
    pass


def enqueue_charge(session: Session, subscription_id: int, now: datetime | None = None) -> Job:
    subscription = session.get(Subscription, subscription_id)
    if subscription is None:
        raise ChargeNowError(f"Подписка {subscription_id} не найдена")
    if not subscription.auto_renew or not subscription.billing_enabled:
        raise ChargeNowError("Автопродление или billing_enabled выключены")
    if subscription.status in (SubscriptionStatus.expired, SubscriptionStatus.cancelled):
        raise ChargeNowError(f"Подписка в статусе {subscription.status.value}")

    return jobs.enqueue(
        session,
        "charge",
        now or datetime.now(ZoneInfo(settings.tz)),
        {"subscription_id": subscription.id},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поставить charge-задачу на now")
    parser.add_argument("--subscription", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        with SessionLocal() as session:
            job = enqueue_charge(session, args.subscription)
            session.commit()
    except ChargeNowError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(
        f"charge job {job.id} enqueued for subscription {args.subscription} "
        f"at {job.run_at.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
