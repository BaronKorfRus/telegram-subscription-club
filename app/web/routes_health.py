"""GET /healthz: короткий ответ всем, подробный — по ключу HEALTHZ_TOKEN."""

import hmac
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine
from app.models import Job, JobStatus, JobType, Payment, PaymentStatus
from app.services import health as health_service

logger = logging.getLogger(__name__)

router = APIRouter()

COMPONENTS = ("db", "bot", "billing", "jobs", "payments")
OVERDUE_AFTER = timedelta(minutes=15)
FAILED_WINDOW = timedelta(hours=24)
STUCK_AFTER = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((_now() - _aware(value)).total_seconds()))


def _max_timestamp(conn: Connection, table: str, column: str) -> datetime | None:
    """Возвращает max(column) или None, если таблицы ещё нет/запрос не удался."""
    try:
        with conn.begin_nested():
            result = conn.execute(text(f"SELECT max({column}) FROM {table}")).scalar_one()
    except SQLAlchemyError:
        return None
    return result


def _token_matches(request: Request) -> bool:
    """Сверяет Bearer-токен с настройкой; пустая настройка закрывает отчёт всем."""
    token = settings.healthz_token
    if not token:
        return False
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())


def _probe() -> tuple[bool, int | None, int | None, dict[str, dict]]:
    billing_age_sec = None
    bot_age_sec = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            billing_age_sec = _age_seconds(_max_timestamp(conn, "payment_events", "received_at"))
            bot_age_sec = _age_seconds(_max_timestamp(conn, "messages", "created_at"))
    except SQLAlchemyError as exc:
        logger.warning("healthz: база недоступна: %s", type(exc).__name__)
        return False, None, None, {}

    try:
        with SessionLocal() as session:
            services = health_service.status(session)
    except Exception:
        logger.exception("healthz: проверка сервисов упала")
        services = {name: {"ok": False, "age_sec": None} for name in health_service.SERVICES}
    return True, billing_age_sec, bot_age_sec, services


def _services_components(services: dict[str, dict]) -> dict[str, dict]:
    limit = settings.health_stale_seconds
    result = {}
    for name in health_service.SERVICES:
        item = services.get(name, {"ok": False, "age_sec": None})
        result[name] = {
            "status": "ok" if item.get("ok") else "fail",
            "age_sec": item.get("age_sec"),
            "limit_sec": limit,
        }
    return result


def _jobs_component(session: Session) -> dict:
    now = _now()
    overdue = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status.in_((JobStatus.pending, JobStatus.running)),
                Job.run_at < now - OVERDUE_AFTER,
                or_(Job.locked_until.is_(None), Job.locked_until <= now),
            )
        )
        or 0
    )
    # У jobs нет updated_at: время терминального падения — это его run_at.
    failed_24h = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.failed, Job.run_at >= now - FAILED_WINDOW)
        )
        or 0
    )
    next_charge = session.scalar(
        select(func.min(Job.run_at)).where(
            Job.type == JobType.charge, Job.status == JobStatus.pending
        )
    )
    if overdue > 0:
        status = "fail"
    elif failed_24h > 0:
        status = "warn"
    else:
        status = "ok"
    next_charge_in_sec = (
        None if next_charge is None else max(0, int((_aware(next_charge) - now).total_seconds()))
    )
    return {
        "status": status,
        "overdue": overdue,
        "failed_24h": failed_24h,
        "next_charge_in_sec": next_charge_in_sec,
    }


def _payments_component(session: Session) -> dict:
    now = _now()
    rejected_24h = (
        session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(
                Payment.status == PaymentStatus.rejected,
                Payment.initiated_by_worker.is_(True),
                Payment.created_at >= now - FAILED_WINDOW,
            )
        )
        or 0
    )
    stuck = (
        session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(
                Payment.status.in_((PaymentStatus.pending, PaymentStatus.created)),
                Payment.created_at < now - STUCK_AFTER,
            )
        )
        or 0
    )
    status = "warn" if rejected_24h or stuck else "ok"
    return {"status": status, "rejected_24h": rejected_24h, "stuck": stuck}


def _checked(name: str, check: Callable[[Session], dict]) -> dict:
    """Своя сессия и свой try на каждую проверку: упала — только она."""
    try:
        with SessionLocal() as session:
            return check(session)
    except Exception:
        logger.exception("healthz: проверка %s упала", name)
        return {"status": "fail"}


def _build_components(db_ok: bool, services: dict[str, dict]) -> dict[str, dict]:
    if not db_ok:
        return {name: {"status": "fail"} for name in COMPONENTS}
    components: dict[str, dict] = {"db": {"status": "ok"}}
    try:
        components.update(_services_components(services))
    except Exception:
        logger.exception("healthz: проверка сервисов упала")
        components.update({name: {"status": "fail"} for name in ("bot", "billing")})
    components["jobs"] = _checked("jobs", _jobs_component)
    components["payments"] = _checked("payments", _payments_component)
    return components


def _summary(components: dict[str, dict]) -> str:
    states = [item["status"] for item in components.values()]
    if "fail" in states:
        return "fail"
    if "warn" in states:
        return "warn"
    return "ok"


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    db_ok, billing_age_sec, bot_age_sec, services = _probe()
    max_billing_age = int(os.getenv("HEALTH_MAX_BILLING_AGE_SEC", "0") or 0)
    max_bot_age = int(os.getenv("HEALTH_MAX_BOT_AGE_SEC", "0") or 0)
    stale = (
        max_billing_age > 0 and billing_age_sec is not None and billing_age_sec > max_billing_age
    ) or (max_bot_age > 0 and bot_age_sec is not None and bot_age_sec > max_bot_age)
    healthy = db_ok and health_service.is_healthy(services) and not stale

    if not _token_matches(request):
        return JSONResponse(
            {"status": "ok" if healthy else "error"},
            status_code=200 if healthy else 503,
        )

    components = _build_components(db_ok, services)
    payload = {
        "status": _summary(components),
        "db": db_ok,
        "services": services,
        "billing_age_sec": billing_age_sec,
        "bot_age_sec": bot_age_sec,
        "problems": [name for name, item in components.items() if item["status"] != "ok"],
        "components": components,
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)
