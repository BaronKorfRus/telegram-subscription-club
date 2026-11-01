import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Client, Subscription, SubscriptionStatus, Tariff
from app.seed import seed_tariffs

logger = logging.getLogger(__name__)
# httpx на INFO логирует полный URL, включая api_key в path.
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_API_BASE = "https://chatter.salebot.pro/api"
TARIFF_CODES = {
    1: "m1_legacy",
    2: "m3_legacy",
    3: "m6_legacy",
    4: "m1",
    5: "m3",
    6: "m6",
}


class MigrationError(RuntimeError):
    pass


@dataclass
class MappedClient:
    tg_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None
    tariff_code: str
    current_period_end: date
    status: str
    billing_enabled: bool
    rebill_id: str | None
    card_mask: str | None = None


@dataclass
class FetchResult:
    clients: list[dict] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Report:
    requested: int
    found: int
    skipped: int
    rows: list[MappedClient] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: int | None = None

    def _missing(self, attr: str) -> str:
        ids = [str(row.tg_id) for row in self.rows if getattr(row, attr) is None]
        return ", ".join(ids) if ids else "—"

    def render(self) -> str:
        migrated = "Перенесено" if self.applied is not None else "Перенесётся"
        count = self.applied if self.applied is not None else len(self.rows)
        return (
            f"Найдено: {self.found}\n"
            f"{migrated}: {count}\n"
            f"Пропущено: {self.skipped}\n"
            f"Не найдено client_id: {', '.join(self.not_found) if self.not_found else '—'}\n"
            f"Без rebill: {self._missing('rebill_id')}\n"
            f"Без телефона: {self._missing('phone')}\n"
            f"Без email: {self._missing('email')}"
            + (
                ""
                if not self.errors
                else "\nОшибки get_variables:\n" + "\n".join(f"- {e}" for e in self.errors)
            )
        )


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _today() -> date:
    return _now().date()


def read_ids(path: str) -> list[str]:
    ids = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            value = line.strip()
            if value and not value.startswith("#"):
                ids.append(value)
    return ids


def _raw_value(raw: dict, key: str):
    if raw.get(key) not in (None, ""):
        return raw[key]
    variables = raw.get("variables") or raw.get("vars") or {}
    return variables.get(key)


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d.%m.%y",
        "%d/%m/%Y",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    parts = name.strip().split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def map_client(raw: dict, today: date) -> MappedClient | None:
    """Преобразует клиента Salebot в строку для clients/subscriptions."""
    platform_id = _raw_value(raw, "platform_id")
    try:
        tg_id = int(str(platform_id).strip())
    except (TypeError, ValueError):
        return None

    tariff_raw = _raw_value(raw, "tarif")
    try:
        tariff_code = TARIFF_CODES[int(str(tariff_raw).strip())]
    except (KeyError, TypeError, ValueError):
        return None

    period_end = _parse_date(_raw_value(raw, "datespis"))
    if period_end is None:
        return None
    if period_end > today:
        status = SubscriptionStatus.active.value
        billing_enabled = True
    elif period_end.year == today.year and period_end.month == 10:
        status = SubscriptionStatus.past_due.value
        billing_enabled = False
    else:
        return None

    full_name = _raw_value(raw, "full_name") or raw.get("name")
    first_name, last_name = _split_name(full_name)
    username = _raw_value(raw, "tg_username") or _raw_value(raw, "username")
    if username:
        username = str(username).strip().lstrip("@") or None
    email = _raw_value(raw, "email")
    phone = _raw_value(raw, "phone")
    rebill = _raw_value(raw, "tinkoff_rebill_id") or _raw_value(raw, "rebill_id")
    card_mask = (
        _raw_value(raw, "card_mask")
        or _raw_value(raw, "tinkoff_card_mask")
        or _raw_value(raw, "mask_card")
    )

    return MappedClient(
        tg_id=tg_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        phone=str(phone).strip() if phone else None,
        email=str(email).strip().lower() if email else None,
        tariff_code=tariff_code,
        current_period_end=period_end,
        status=status,
        billing_enabled=billing_enabled,
        rebill_id=str(rebill).strip() if rebill else None,
        card_mask=str(card_mask).strip() if card_mask else None,
    )


def build_report(requested: int, result: FetchResult, rows: list[MappedClient]) -> Report:
    return Report(
        requested=requested,
        found=len(result.clients),
        skipped=len(result.clients) - len(rows),
        rows=rows,
        not_found=result.not_found,
        errors=result.errors,
    )


def apply_rows(session: Session, rows: list[MappedClient], now: datetime | None = None) -> int:
    """Upsert клиентов и подписок по tg_id; повторный вызов не создаёт дублей."""
    now = now or _now()
    seed_tariffs(session)
    tariffs = {t.code: t for t in session.scalars(select(Tariff))}

    for row in rows:
        tariff = tariffs.get(row.tariff_code)
        if tariff is None:
            raise MigrationError(f"Тариф {row.tariff_code} не найден")
        client = session.scalar(select(Client).where(Client.tg_id == row.tg_id))
        if client is None:
            client = Client(tg_id=row.tg_id)
            session.add(client)
        client.username = row.username
        client.first_name = row.first_name
        client.last_name = row.last_name
        client.phone = row.phone
        client.email = row.email
        session.flush()

        subscription = session.scalar(
            select(Subscription)
            .where(Subscription.client_id == client.id)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
        if subscription is None:
            subscription = Subscription(client=client, tariff=tariff, started_at=now)
            session.add(subscription)
        subscription.tariff = tariff
        subscription.status = SubscriptionStatus(row.status)
        subscription.current_period_end = row.current_period_end
        subscription.auto_renew = True
        subscription.billing_enabled = row.billing_enabled
        subscription.rebill_id = row.rebill_id
        subscription.card_mask = row.card_mask
        subscription.failed_attempts = 0
        session.flush()

    return len(rows)


def _api_url(base: str, api_key: str, action: str) -> str:
    return f"{base.rstrip('/')}/{api_key}/{action}"


def _extract_items(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("result", "clients", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise MigrationError("Неожиданный ответ Salebot API")


def _fetch_variables(base: str, api_key: str, client_id: int | str) -> dict:
    url = _api_url(base, api_key, f"get_variables?client_id={client_id}")
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MigrationError(f"Не удалось получить переменные клиента {client_id}") from exc
    if isinstance(data, dict):
        for key in ("result", "variables"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return {key: value for key, value in data.items() if key not in {"status"}}
    return {}


def fetch_clients(ids: list[str]) -> FetchResult:
    api_key = os.environ.get("SALEBOT_API_KEY", "").strip()
    base_url = os.environ.get("SALEBOT_API_BASE", DEFAULT_API_BASE).strip()
    if not api_key:
        raise MigrationError("SALEBOT_API_KEY не задан")

    url = _api_url(base_url, api_key, "get_clients?limit=500")
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        items = _extract_items(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise MigrationError("Не удалось получить клиентов из Salebot") from exc

    by_id = {str(item.get("id")): item for item in items}
    result = FetchResult()
    for raw_id in ids:
        client_id = str(raw_id).strip()
        item = by_id.get(client_id)
        if item is None:
            result.not_found.append(client_id)
            continue
        variables = dict(item.get("variables") or {})
        try:
            variables.update(_fetch_variables(base_url, api_key, client_id))
        except MigrationError as exc:
            # Один сбой не обрывает перенос остальных.
            result.errors.append(f"{client_id}: {exc}")
        merged = dict(item)
        merged["variables"] = variables
        result.clients.append(merged)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос клиенток из Salebot")
    parser.add_argument("--ids", required=True, help="файл с platform_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        ids = read_ids(args.ids)
        result = fetch_clients(ids)
        today = _today()
        rows = [row for row in (map_client(item, today) for item in result.clients) if row]
    except (MigrationError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    report = build_report(len(ids), result, rows)
    if args.dry_run:
        print(report.render())
        return 0

    try:
        with SessionLocal() as session:
            applied = apply_rows(session, rows)
            session.commit()
    except Exception as exc:
        print(f"Ошибка применения: {exc}", file=sys.stderr)
        return 1

    report.applied = applied
    print(report.render())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
