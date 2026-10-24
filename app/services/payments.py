import json
import logging
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import receipts, tbank, texts
from app.config import settings
from app.models import (
    Client,
    JobType,
    Payment,
    PaymentEvent,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.services import jobs, post_payment, subscriptions
from app.tbank import check_token

logger = logging.getLogger(__name__)


class PaymentError(RuntimeError):
    """Не удалось создать платёж в Т-Банке."""


def _today() -> date:
    return datetime.now(ZoneInfo(settings.tz)).date()


def open_subscription(
    session: Session, client: Client, today: date | None = None
) -> Subscription | None:
    """Подписка с ещё не истёкшим доступом (включая отменённую до конца периода)."""
    today = today or _today()
    candidates = session.scalars(
        select(Subscription)
        .where(Subscription.client_id == client.id)
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
    )
    return next((s for s in candidates if subscriptions.has_access(s, today)), None)


def offered_tariffs(session: Session, client: Client, today: date | None = None) -> list[Tariff]:
    """Открытая подписка — только её тариф; иначе только активные тарифы."""
    subscription = open_subscription(session, client, today)
    if subscription is not None and subscription.tariff is not None:
        return [subscription.tariff]
    return list(
        session.scalars(
            select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.sort_order, Tariff.id)
        )
    )


def legacy_subscription(session: Session, client: Client) -> Subscription | None:
    """Действующая/просроченная legacy-подписка, сохраняющая старую цену."""
    return session.scalar(
        select(Subscription)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .where(
            Subscription.client_id == client.id,
            Tariff.is_legacy.is_(True),
            Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.past_due)),
        )
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
        .limit(1)
    )


def renewal_price(session: Session, subscription: Subscription) -> int:
    tariff = subscription.tariff or session.get(Tariff, subscription.tariff_id)
    if tariff is None:
        raise PaymentError("У подписки не найден тариф")
    return tariff.price_kopecks


def payment_description(tariff: Tariff) -> str:
    # Т-Банк не любит двойные кавычки в описании.
    template = texts.PAYMENT_DESCRIPTION
    return template.format(brand=texts.BRAND, title=tariff.title).replace('"', "")


def _reject_payment(payment: Payment, code: str | None, message: str | None) -> None:
    payment.status = PaymentStatus.rejected
    payment.error_code = code
    payment.error_message = message


def create_payment(
    session: Session,
    client: Client,
    tariff: Tariff,
    kind: PaymentKind | str,
    subscription: Subscription | None = None,
    amount_kopecks: int | None = None,
    description: str | None = None,
    receipt: dict | None = None,
) -> tuple[Payment, str]:
    payment_kind = kind if isinstance(kind, PaymentKind) else PaymentKind(kind)
    order_id = f"{client.tg_id}-{uuid4().hex[:10]}"
    amount = tariff.price_kopecks if amount_kopecks is None else amount_kopecks
    payment_receipt = (
        receipt if receipt is not None else receipts.build(tariff, client.email, client.phone)
    )
    bank_description = (description or payment_description(tariff)).replace('"', "")

    # Строка должна существовать в БД до обращения в банк: если Init упадёт,
    # останется rejected-платёж для разбора, а не «платёж-призрак».
    payment = Payment(
        client_id=client.id,
        subscription_id=subscription.id if subscription is not None else None,
        tariff_id=tariff.id,
        kind=payment_kind,
        amount_kopecks=amount,
        order_id=order_id,
        status=PaymentStatus.created,
        receipt_json=payment_receipt,
    )
    session.add(payment)
    session.commit()

    try:
        result = tbank.init_payment(
            amount_kopecks=amount,
            order_id=order_id,
            description=bank_description,
            customer_key=str(client.tg_id),
            recurrent=True,
            receipt=payment_receipt,
            notification_url=settings.tbank_notification_url or None,
        )
    except tbank.TbankError as exc:
        _reject_payment(payment, "network", str(exc))
        session.commit()
        raise PaymentError(f"Init: {exc}") from exc

    if not result.get("Success"):
        _reject_payment(
            payment,
            str(result["ErrorCode"]) if result.get("ErrorCode") is not None else None,
            str(result.get("Message") or result.get("Details") or ""),
        )
        session.commit()
        error_code = payment.error_code or "unknown"
        raise PaymentError(f"Init отклонён: {error_code}")

    payment_url = result.get("PaymentURL")
    payment_id = result.get("PaymentId")
    if not payment_url or not payment_id:
        _reject_payment(payment, "invalid_response", "Init вернул ответ без PaymentURL/PaymentId")
        session.commit()
        raise PaymentError("Init вернул ответ без PaymentURL/PaymentId")

    payment.bank_payment_id = str(payment_id)
    payment.status = PaymentStatus.pending
    payment.error_code = None
    payment.error_message = None
    session.commit()
    return payment, str(payment_url)


def create_tariff_payment(
    session: Session,
    client: Client,
    code: str,
    today: date | None = None,
) -> tuple[Payment, str]:
    """Платёж по кнопке тарифа: открытую подписку продлеваем, иную — создаём."""
    subscription = open_subscription(session, client, today)
    if subscription is not None and subscription.tariff is not None:
        if subscription.tariff.code != code:
            raise PaymentError("Тариф недоступен для этой подписки")
        return create_payment(
            session,
            client,
            subscription.tariff,
            PaymentKind.renewal,
            subscription=subscription,
        )

    tariff = next((t for t in offered_tariffs(session, client, today) if t.code == code), None)
    if tariff is None:
        raise PaymentError("Тариф недоступен")
    return create_payment(session, client, tariff, PaymentKind.initial)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Moscow"))


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _store_event(session: Session, payload: dict, signature_ok: bool) -> PaymentEvent:
    event = PaymentEvent(
        body=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        signature_ok=signature_ok,
    )
    session.add(event)
    session.flush()
    return event


def _find_payment(session: Session, payload: dict) -> Payment | None:
    payment_id = payload.get("PaymentId")
    if payment_id:
        payment = session.scalar(
            select(Payment).where(Payment.bank_payment_id == str(payment_id)).with_for_update()
        )
        if payment is not None:
            return payment

    order_id = payload.get("OrderId")
    if order_id:
        return session.scalar(
            select(Payment).where(Payment.order_id == str(order_id)).with_for_update()
        )
    return None


def _mark_rejected(payment: Payment, payload: dict) -> bool:
    """Отказ банка: платёж rejected. Возвращает True только при первом отказе."""
    if payment.status == PaymentStatus.confirmed:
        logger.error(
            "T-Bank rejection for already confirmed payment: order_id=%s",
            payment.order_id,
        )
        return False
    if payment.status == PaymentStatus.rejected:
        return False

    payment.status = PaymentStatus.rejected
    error_code = payload.get("ErrorCode")
    payment.error_code = str(error_code) if error_code is not None else None
    message = payload.get("Message") or payload.get("ErrorMessage")
    payment.error_message = str(message) if message is not None else None
    return True


def _apply_card_data(subscription: Subscription, payload: dict) -> None:
    rebill_id = payload.get("RebillId")
    if rebill_id:
        subscription.rebill_id = str(rebill_id)
    pan = payload.get("Pan")
    if pan:
        subscription.card_mask = str(pan)


def handle_notification(
    session: Session,
    payload: dict,
    now: datetime | None = None,
) -> PaymentEvent:
    """Обрабатывает уведомление банка и всегда сохраняет PaymentEvent.

    Транзакцию коммитит вызывающий код. Идемпотентность — по PaymentId:
    повторное уведомление не продлевает подписку второй раз.
    """
    signature_ok = check_token(payload, settings.tbank_password)
    event = _store_event(session, payload, signature_ok)
    if not signature_ok:
        logger.warning("T-Bank notification with invalid signature")
        return event

    payment = _find_payment(session, payload)
    if payment is None:
        logger.warning(
            "T-Bank notification for unknown payment: PaymentId=%s OrderId=%s",
            payload.get("PaymentId"),
            payload.get("OrderId"),
        )
        return event

    if payment.bank_payment_id is None and payload.get("PaymentId"):
        payment.bank_payment_id = str(payload["PaymentId"])

    moment = now or _now()
    status = str(payload.get("Status", "")).upper()
    if status in {"REJECTED", "CANCELED", "CANCELLED"}:
        newly_rejected = _mark_rejected(payment, payload)
        if (
            newly_rejected
            and not payment.initiated_by_worker
            and payment.kind in (PaymentKind.initial, PaymentKind.renewal)
        ):
            jobs.enqueue(
                session,
                JobType.payment_failed.value,
                moment,
                {"payment_id": payment.id},
            )
        session.flush()
        return event

    if status != "CONFIRMED":
        return event

    amount = _as_int(payload.get("Amount"))
    if amount != payment.amount_kopecks:
        logger.error(
            "T-Bank amount mismatch: order_id=%s payment_id=%s expected=%s got=%s",
            payment.order_id,
            payload.get("PaymentId"),
            payment.amount_kopecks,
            payload.get("Amount"),
        )
        return event

    if payment.status == PaymentStatus.confirmed:
        return event

    if payment.kind == PaymentKind.initial:
        if payment.tariff is None:
            logger.error(
                "T-Bank confirmed initial payment without tariff: order_id=%s",
                payment.order_id,
            )
            return event
        subscription = subscriptions.activate(
            session, payment.client, payment.tariff, payment, moment
        )
        _apply_card_data(subscription, payload)
        from app.services import billing

        billing.schedule_cycle(session, subscription)
    elif payment.kind == PaymentKind.renewal:
        if payment.subscription is None:
            logger.error(
                "T-Bank confirmed renewal without subscription: order_id=%s",
                payment.order_id,
            )
            return event
        subscriptions.extend(session, payment.subscription, payment, moment)
        _apply_card_data(payment.subscription, payload)
        payment.subscription.auto_renew = True
        from app.services import billing

        billing.schedule_cycle(session, payment.subscription)
    elif payment.kind == PaymentKind.card_change:
        if payment.subscription is None:
            logger.error(
                "T-Bank confirmed card change without subscription: order_id=%s",
                payment.order_id,
            )
            return event
        _apply_card_data(payment.subscription, payload)

    payment.status = PaymentStatus.confirmed
    payment.confirmed_at = moment
    if payment.kind in (PaymentKind.initial, PaymentKind.renewal):
        post_payment.enqueue(session, payment, moment)
    session.flush()
    return event
