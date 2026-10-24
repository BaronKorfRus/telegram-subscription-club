import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import texts
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Admin,
    AdminMessageLink,
    AdminStatus,
    Client,
    Message,
    MessageDirection,
    MessageKind,
    NotificationError,
    Subscription,
)

logger = logging.getLogger(__name__)

SERVICE_REPLY_TEXT = texts.OUT_OF_FLOW_ACK
SERVICE_REPLY_COOLDOWN = timedelta(minutes=10)
ADMIN_REPLY_HINT = texts.ADMIN_REPLY_HINT


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def get_or_create_client(session: Session, tg_id: int, username: str | None = None) -> Client:
    client = session.scalar(select(Client).where(Client.tg_id == tg_id))
    if client is None:
        client = Client(tg_id=tg_id, username=username)
        session.add(client)
        session.flush()
    elif username and client.username != username:
        client.username = username
    return client


def latest_subscription(session: Session, client_id: int) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .where(Subscription.client_id == client_id)
        .order_by(Subscription.current_period_end.desc(), Subscription.id.desc())
        .limit(1)
    )


def store_client_message(
    session: Session,
    tg_id: int,
    text: str | None,
    tg_message_id: int | None = None,
    username: str | None = None,
    now: datetime | None = None,
) -> tuple[Client, Message, bool]:
    """Сохраняет сообщение клиента; возвращает (client, message, слать ли ответ)."""
    now = now or _now()
    client = get_or_create_client(session, tg_id, username)
    message = Message(
        client_id=client.id,
        direction=MessageDirection.inbound,
        kind=MessageKind.client,
        text=text or "",
        tg_message_id=tg_message_id,
        is_in_flow=False,
        created_at=now,
    )
    session.add(message)
    session.flush()

    last_service = session.scalar(
        select(Message)
        .where(
            Message.client_id == client.id,
            Message.direction == MessageDirection.outbound,
            Message.kind == MessageKind.service,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    send_reply = last_service is None or last_service.created_at <= now - SERVICE_REPLY_COOLDOWN
    if send_reply:
        session.add(
            Message(
                client_id=client.id,
                direction=MessageDirection.outbound,
                kind=MessageKind.service,
                text=SERVICE_REPLY_TEXT,
                is_in_flow=False,
                created_at=now,
            )
        )
        session.flush()
    return client, message, send_reply


def build_admin_context(client: Client, subscription: Subscription | None) -> str:
    name = " ".join(filter(None, [client.first_name, client.last_name]))
    if not name:
        name = client.username or str(client.tg_id)
    username = f"@{client.username}" if client.username else "—"
    if subscription is not None:
        tariff = subscription.tariff.title if subscription.tariff is not None else "—"
        status = subscription.status.value
        period_end = subscription.current_period_end.strftime("%d.%m.%Y")
    else:
        tariff = "—"
        status = "нет подписки"
        period_end = "—"
    return (
        "Сообщение от клиента\n"
        f"Имя: {name}\n"
        f"Username: {username}\n"
        f"Тариф: {tariff}\n"
        f"Статус: {status}\n"
        f"Доступ до: {period_end}"
    )


def _add_admin_link(
    session: Session,
    admin_tg_id: int,
    message_id: int,
    client: Client,
    message: Message,
    now: datetime | None,
) -> AdminMessageLink:
    link = AdminMessageLink(
        admin_tg_id=admin_tg_id,
        forwarded_message_id=message_id,
        client_id=client.id,
        client_message_id=message.id,
        created_at=now or _now(),
    )
    session.add(link)
    return link


async def forward_to_admins(
    bot,
    session: Session,
    client: Client,
    message: Message,
    admins: list[Admin],
    now: datetime | None = None,
) -> list[AdminMessageLink]:
    """Отправляет админам текст с контекстом; вложение копирует по возможности.

    forward_message не используем: он падает на удалённых/служебных сообщениях
    и анонимных админах. Ответ админа привязываем к id отправленного текста
    (и копии вложения, если она удалась).
    """
    subscription = latest_subscription(session, client.id)
    context = build_admin_context(client, subscription)
    body = f"{context}\n\nСообщение клиента:\n{message.text or '[вложение]'}"
    if len(body) > 4_000:
        body = body[:3_990] + "…"

    links: list[AdminMessageLink] = []
    for admin in admins:
        try:
            sent = await bot.send_message(admin.tg_id, body, parse_mode=None)
        except TelegramAPIError:
            logger.warning("Не удалось отправить обращение админу tg_id=%s", admin.tg_id)
            continue

        links.append(_add_admin_link(session, admin.tg_id, sent.message_id, client, message, now))

        if not message.text and message.tg_message_id:
            try:
                copied = await bot.copy_message(
                    chat_id=admin.tg_id,
                    from_chat_id=client.tg_id,
                    message_id=message.tg_message_id,
                )
            except TelegramAPIError:
                logger.warning("Не удалось скопировать вложение админу tg_id=%s", admin.tg_id)
            else:
                links.append(
                    _add_admin_link(
                        session,
                        admin.tg_id,
                        copied.message_id,
                        client,
                        message,
                        now,
                    )
                )
    session.flush()
    return links


async def send_to_admins(bot, admins: list[Admin], text: str, reply_markup=None) -> int:
    """Рассылка админам: ошибка одному не прерывает остальных."""
    sent = 0
    for admin in admins:
        try:
            await bot.send_message(admin.tg_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            logger.exception("Не удалось отправить сообщение админу tg_id=%s", admin.tg_id)
            continue
        sent += 1
    return sent


def admin_status(session: Session, tg_id: int) -> str | None:
    admin = session.scalar(select(Admin).where(Admin.tg_id == tg_id))
    return admin.status.value if admin is not None else None


def is_admin(session: Session, tg_id: int) -> bool:
    return admin_status(session, tg_id) == AdminStatus.active.value


def is_owner(session: Session, tg_id: int) -> bool:
    admin = session.scalar(select(Admin).where(Admin.tg_id == tg_id))
    return bool(admin is not None and admin.is_owner and admin.status == AdminStatus.active)


def active_admins(session: Session) -> list[Admin]:
    return list(
        session.scalars(select(Admin).where(Admin.status == AdminStatus.active).order_by(Admin.id))
    )


def ensure_owner(session: Session, owner_tg_id: int | None = None) -> Admin | None:
    owner_tg_id = owner_tg_id if owner_tg_id is not None else settings.owner_tg_id
    if owner_tg_id is None:
        return None
    admin = session.scalar(select(Admin).where(Admin.tg_id == owner_tg_id))
    if admin is None:
        admin = Admin(
            tg_id=owner_tg_id,
            name="Владелец",
            is_owner=True,
            status=AdminStatus.active,
        )
        session.add(admin)
    else:
        admin.is_owner = True
        admin.status = AdminStatus.active
    session.flush()
    return admin


def handle_admin_word(session: Session, tg_id: int, name: str | None = None) -> Admin:
    admin = session.scalar(select(Admin).where(Admin.tg_id == tg_id))
    if admin is not None:
        return admin
    admin = Admin(tg_id=tg_id, name=name, status=AdminStatus.pending)
    session.add(admin)
    session.flush()
    return admin


def confirm_admin(session: Session, tg_id: int, added_by: int | None = None) -> Admin | None:
    admin = session.scalar(select(Admin).where(Admin.tg_id == tg_id))
    if admin is None:
        return None
    admin.status = AdminStatus.active
    if added_by is not None:
        admin.added_by = added_by
    session.flush()
    return admin


def reject_admin(session: Session, tg_id: int) -> Admin | None:
    admin = session.scalar(select(Admin).where(Admin.tg_id == tg_id))
    if admin is None or admin.status != AdminStatus.pending:
        return None
    session.delete(admin)
    session.flush()
    return admin


def handle_admin_reply(
    session: Session,
    admin_tg_id: int,
    reply_to_message_id: int,
    text: str,
    now: datetime | None = None,
) -> int | None:
    """Находит клиента по пересланному сообщению и сохраняет ответ админа."""
    link = session.scalar(
        select(AdminMessageLink).where(
            AdminMessageLink.admin_tg_id == admin_tg_id,
            AdminMessageLink.forwarded_message_id == reply_to_message_id,
        )
    )
    if link is None:
        return None
    client = session.get(Client, link.client_id)
    if client is None:
        return None
    session.add(
        Message(
            client_id=client.id,
            direction=MessageDirection.outbound,
            kind=MessageKind.admin,
            text=text,
            is_in_flow=False,
            created_at=now or _now(),
        )
    )
    session.flush()
    return client.tg_id


def record_delivery_error(
    tg_id: int, text: str, error: object, now: datetime | None = None
) -> None:
    """Сохраняет ошибку доставки, чтобы сводка могла её показать."""
    try:
        with SessionLocal() as session:
            session.add(
                NotificationError(
                    tg_id=tg_id,
                    text=text,
                    error=str(error),
                    created_at=now or _now(),
                )
            )
            session.commit()
    except Exception:
        logger.exception("Не удалось сохранить ошибку доставки tg_id=%s", tg_id)
