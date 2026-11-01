"""Живость фоновых сервисов: отметки в БД и их проверка.

Бот и биллинг раз в 30 секунд пишут отметку `service_heartbeats`. `/healthz`
сравнивает отметку с текущим временем и отдаёт 503, если сервис замолчал.
Это единственный способ заметить, что процесс жив, а цикл — нет.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ServiceHeartbeat

logger = logging.getLogger(__name__)

BOT = "bot"
BILLING = "billing"
SERVICES = (BOT, BILLING)
BEAT_INTERVAL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def beat(session: Session, name: str, now: datetime | None = None) -> None:
    """Пишет отметку живости; upsert, чтобы не плодить строки."""
    moment = _aware(now or _now())
    statement = (
        insert(ServiceHeartbeat)
        .values(name=name, beat_at=moment)
        .on_conflict_do_update(index_elements=[ServiceHeartbeat.name], set_={"beat_at": moment})
    )
    session.execute(statement)


def beat_own_session(name: str, now: datetime | None = None) -> None:
    """Отметка из фонового процесса: своя короткая сессия и коммит."""
    from app.db import SessionLocal

    with SessionLocal() as session:
        beat(session, name, now)
        session.commit()


def _age_seconds(beat_at: datetime | None, now: datetime) -> int | None:
    if beat_at is None:
        return None
    return max(0, int((now - _aware(beat_at)).total_seconds()))


def status(session: Session, now: datetime | None = None) -> dict[str, dict]:
    """Состояние каждого сервиса: `{"bot": {"ok": True, "age_sec": 4}}`."""
    moment = _aware(now or _now())
    limit = settings.health_stale_seconds
    rows = {
        row.name: row.beat_at
        for row in session.scalars(
            select(ServiceHeartbeat).where(ServiceHeartbeat.name.in_(SERVICES))
        )
    }
    result: dict[str, dict] = {}
    for name in SERVICES:
        age = _age_seconds(rows.get(name), moment)
        result[name] = {"ok": age is not None and age <= limit, "age_sec": age}
    return result


def is_healthy(services: dict[str, dict]) -> bool:
    return all(item["ok"] for item in services.values())
