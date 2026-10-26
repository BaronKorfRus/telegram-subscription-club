from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db_base import Base


class SubscriptionStatus(StrEnum):
    active = "active"
    past_due = "past_due"
    cancelled = "cancelled"
    expired = "expired"


class PaymentStatus(StrEnum):
    created = "created"
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"
    cancelled = "cancelled"


class PaymentKind(StrEnum):
    initial = "initial"
    renewal = "renewal"
    card_change = "card_change"
    refund = "refund"


class JobStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class JobType(StrEnum):
    reminder_3d = "reminder_3d"
    reminder_1d = "reminder_1d"
    charge = "charge"
    expire = "expire"
    kick = "kick"
    reconcile = "reconcile"
    post_payment = "post_payment"
    payment_failed = "payment_failed"
    daily_report = "daily_report"
    expire_check = "expire_check"
    window_soon = "window_soon"
    window_open = "window_open"


class MessageDirection(StrEnum):
    inbound = "in"
    outbound = "out"


class MessageKind(StrEnum):
    client = "client"
    bot = "bot"
    admin = "admin"
    service = "service"


class AdminStatus(StrEnum):
    pending = "pending"
    active = "active"


def _enum(enum_cls: type[StrEnum], name: str, length: int = 16):
    """Строковый enum в VARCHAR; в базе лежат value, а не имена членов."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="client")
    payments: Mapped[list[Payment]] = relationship(back_populates="client")
    messages: Mapped[list[Message]] = relationship(back_populates="client")


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    period_months: Mapped[int] = mapped_column(Integer, nullable=False)
    is_legacy: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="tariff")
    payments: Mapped[list[Payment]] = relationship(back_populates="tariff")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, "subscription_status"),
        default=SubscriptionStatus.active,
        server_default=SubscriptionStatus.active.value,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    forced_period_end: Mapped[date | None] = mapped_column(Date)
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    billing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    rebill_id: Mapped[str | None] = mapped_column(String(64))
    card_mask: Mapped[str | None] = mapped_column(String(32))
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    client: Mapped[Client] = relationship(back_populates="subscriptions")
    tariff: Mapped[Tariff] = relationship(back_populates="subscriptions")
    payments: Mapped[list[Payment]] = relationship(back_populates="subscription")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"))
    kind: Mapped[PaymentKind] = mapped_column(_enum(PaymentKind, "payment_kind"), nullable=False)
    amount_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    bank_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"),
        default=PaymentStatus.created,
        server_default=PaymentStatus.created.value,
        nullable=False,
    )
    receipt_json: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    post_payment_done: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    initiated_by_worker: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    client: Mapped[Client] = relationship(back_populates="payments")
    subscription: Mapped[Subscription | None] = relationship(back_populates="payments")
    tariff: Mapped[Tariff | None] = relationship(back_populates="payments")


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    signature_ok: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )


class NotificationError(Base):
    __tablename__ = "notification_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[JobType] = mapped_column(_enum(JobType, "job_type", 32), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"),
        default=JobStatus.pending,
        server_default=JobStatus.pending.value,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        _enum(MessageDirection, "message_direction", 8), nullable=False
    )
    kind: Mapped[MessageKind] = mapped_column(_enum(MessageKind, "message_kind"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    is_in_flow: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    client: Mapped[Client] = relationship(back_populates="messages")


class AdminMessageLink(Base):
    """Связь пересланного админу сообщения с клиентом (для reply-ответов)."""

    __tablename__ = "admin_message_links"
    __table_args__ = (
        UniqueConstraint(
            "admin_tg_id",
            "forwarded_message_id",
            name="uq_admin_message_links_admin_forwarded",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    forwarded_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    client_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    is_owner: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    status: Mapped[AdminStatus] = mapped_column(
        _enum(AdminStatus, "admin_status"),
        default=AdminStatus.pending,
        server_default=AdminStatus.pending.value,
        nullable=False,
    )
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
