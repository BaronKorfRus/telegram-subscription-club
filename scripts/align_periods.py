"""Выравнивает даты подписок на 22-е число и пересоздаёт задачи цикла.

Использование:
    python -m scripts.align_periods           # dry-run: таблица «было → станет»
    python -m scripts.align_periods --apply   # записать даты и пересоздать задачи

Берутся только `active` и `past_due`. Отменённые и истёкшие не трогаем.
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import SessionLocal
from app.models import Job, JobStatus, JobType, Subscription, SubscriptionStatus
from app.services import billing, subscriptions
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

STALE_JOB_TYPES = (
    JobType.charge.value,
    JobType.reminder_3d.value,
    JobType.reminder_1d.value,
)


@dataclass(frozen=True)
class PlanRow:
    """Что и на какую дату поменяется."""

    subscription_id: int
    client_name: str
    old_end: date
    new_end: date


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _target_end(old_end: date, today: date) -> date:
    """22-е число того же месяца; если оно уже прошло — 22-е следующего."""
    candidate = subscriptions.add_months(old_end.replace(day=1), 0)
    if candidate >= today:
        return candidate
    return subscriptions.add_months(old_end.replace(day=1), 1)


def _client_name(subscription: Subscription) -> str:
    client = subscription.client
    if client is None:
        return str(subscription.client_id)
    name = " ".join(filter(None, [client.first_name, client.last_name])).strip()
    return name or client.username or str(client.tg_id)


def plan(session: Session, today: date) -> list[PlanRow]:
    """Список подписок, у которых дата окончания не на 22-е."""
    rows: list[PlanRow] = []
    subscriptions_list = session.scalars(
        select(Subscription)
        .where(Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)))
        .order_by(Subscription.id)
    )
    for subscription in subscriptions_list:
        new_end = _target_end(subscription.current_period_end, today)
        if new_end == subscription.current_period_end:
            continue
        rows.append(
            PlanRow(
                subscription_id=subscription.id,
                client_name=_client_name(subscription),
                old_end=subscription.current_period_end,
                new_end=new_end,
            )
        )
    return rows


def _clear_stale_jobs(session: Session, subscription_id: int) -> None:
    session.execute(
        delete(Job).where(
            Job.type.in_(STALE_JOB_TYPES),
            Job.status == JobStatus.pending,
            Job.payload["subscription_id"].as_integer() == subscription_id,
        )
    )
    session.query(Job).filter(
        Job.type == JobType.expire_check,
        Job.status == JobStatus.pending,
        Job.payload["subscription_id"].as_integer() == subscription_id,
    ).delete(synchronize_session=False)


def apply(session: Session, rows: list[PlanRow], today: date) -> int:
    """Пишет даты, чистит устаревшие pending-задачи и ставит новый цикл."""
    changed = 0
    for row in rows:
        subscription = session.get(Subscription, row.subscription_id)
        if subscription is None:
            continue
        if subscription.current_period_end == row.new_end:
            # Уже выровнено (повторный запуск): ничего не делаем.
            continue
        subscription.current_period_end = row.new_end
        _clear_stale_jobs(session, subscription.id)
        billing.schedule_cycle(session, subscription)
        changed += 1
    session.flush()
    return changed


def _format_table(rows: list[PlanRow]) -> str:
    if not rows:
        return "Нечего выравнивать: все даты уже на 22-е."
    lines = ["id | клиент | было | станет", "--- | --- | --- | ---"]
    for row in rows:
        lines.append(
            f"{row.subscription_id} | {row.client_name} | "
            f"{row.old_end.isoformat()} | {row.new_end.isoformat()}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Выравнивание дат подписок на 22-е")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать изменения и пересоздать задачи (по умолчанию dry-run)",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="сегодняшняя дата в формате YYYY-MM-DD (по умолчанию — сегодня по МСК)",
    )
    args = parser.parse_args(argv)

    today = args.date or datetime.now(_tz()).date()
    with SessionLocal() as session:
        rows = plan(session, today)
        print(_format_table(rows))
        if not args.apply:
            print(f"\nDry-run: изменилось бы подписок — {len(rows)}.")
            print("Для записи запустите с ключом --apply.")
            return 0
        changed = apply(session, rows, today)
        session.commit()
    print(f"\nВыровнено подписок: {changed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
