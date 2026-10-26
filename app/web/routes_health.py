import os
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal, engine
from app.services import health as health_service

router = APIRouter()


def _age_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


def _max_timestamp(conn: Connection, table: str, column: str) -> datetime | None:
    """Возвращает max(column) или None, если таблицы ещё нет/запрос не удался."""
    try:
        with conn.begin_nested():
            result = conn.execute(text(f"SELECT max({column}) FROM {table}")).scalar_one()
    except SQLAlchemyError:
        return None
    return result


@router.get("/healthz")
def healthz() -> JSONResponse:
    db_ok = False
    billing_age_sec = None
    bot_age_sec = None
    services: dict[str, dict] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            db_ok = True
            billing_age_sec = _age_seconds(_max_timestamp(conn, "payment_events", "received_at"))
            bot_age_sec = _age_seconds(_max_timestamp(conn, "messages", "created_at"))
        with SessionLocal() as session:
            services = health_service.status(session)
    except SQLAlchemyError:
        db_ok = False

    max_billing_age = int(os.getenv("HEALTH_MAX_BILLING_AGE_SEC", "0") or 0)
    max_bot_age = int(os.getenv("HEALTH_MAX_BOT_AGE_SEC", "0") or 0)
    stale = (
        max_billing_age > 0 and billing_age_sec is not None and billing_age_sec > max_billing_age
    ) or (max_bot_age > 0 and bot_age_sec is not None and bot_age_sec > max_bot_age)
    services_ok = db_ok and health_service.is_healthy(services)
    healthy = services_ok and not stale
    payload = {
        "status": "ok" if healthy else "error",
        "db": db_ok,
        "services": services,
        "billing_age_sec": billing_age_sec,
        "bot_age_sec": bot_age_sec,
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)
