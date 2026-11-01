from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from app.bot.middlewares import OutOfFlowMiddleware
from app.models import (
    Admin,
    AdminMessageLink,
    AdminStatus,
    Client,
    Message,
    MessageDirection,
    MessageKind,
    Subscription,
    SubscriptionStatus,
    Tariff,
)
from app.seed import seed_tariffs
from app.services.messages import (
    active_admins,
    admin_status,
    build_admin_context,
    confirm_admin,
    ensure_owner,
    forward_to_admins,
    handle_admin_reply,
    handle_admin_word,
    reject_admin,
    send_to_admins,
    store_client_message,
)

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=MOSCOW)
TG_ID = 930001
ADMIN_TG_ID = 930099
OWNER_TG_ID = 930000


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.copied: list[tuple[int, int, int]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text))
        return FakeMessage(900 + len(self.sent))

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append((chat_id, from_chat_id, message_id))
        return FakeMessage(800 + len(self.copied))


class CopyFailsBot(FakeBot):
    async def copy_message(self, chat_id, from_chat_id, message_id):
        raise TelegramBadRequest(
            method=SendMessage(chat_id=chat_id, text="copy"), message="message to copy not found"
        )


class ForbiddenBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.attempts += 1
        raise TelegramForbiddenError(
            method=SendMessage(chat_id=chat_id, text=text),
            message="Forbidden: bot can't initiate conversation with a user",
        )


@pytest.fixture
def client_row(session):
    seed_tariffs(session)
    client = Client(
        tg_id=TG_ID,
        username="anna",
        first_name="Анна",
        last_name="Смирнова",
        phone="+79991234567",
    )
    tariff = session.query(Tariff).filter_by(code="m1").one()
    subscription = Subscription(
        client=client,
        tariff=tariff,
        status=SubscriptionStatus.active,
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=MOSCOW),
        current_period_end=date(2026, 11, 22),
    )
    session.add(subscription)
    session.flush()
    return client


@pytest.fixture
def admin(session):
    row = Admin(tg_id=ADMIN_TG_ID, name="Админ", status=AdminStatus.active)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def owner(session):
    row = Admin(tg_id=OWNER_TG_ID, name="Владелец", is_owner=True, status=AdminStatus.active)
    session.add(row)
    session.flush()
    return row


def test_store_client_message_and_antiflood(session, client_row):
    _client, first, send_first = store_client_message(
        session, TG_ID, "Первый вопрос", tg_message_id=11, now=NOW
    )
    _client, second, send_second = store_client_message(
        session, TG_ID, "Второй вопрос", tg_message_id=12, now=NOW + timedelta(minutes=1)
    )
    _client, third, send_third = store_client_message(
        session, TG_ID, "Третий вопрос", tg_message_id=13, now=NOW + timedelta(minutes=11)
    )

    assert first.kind == MessageKind.client
    assert first.direction == MessageDirection.inbound
    assert first.is_in_flow is False
    assert second.is_in_flow is False
    assert send_first is True
    assert send_second is False
    assert send_third is True
    assert (
        session.query(Message).filter_by(client_id=client_row.id, kind=MessageKind.client).count()
        == 3
    )
    assert (
        session.query(Message).filter_by(client_id=client_row.id, kind=MessageKind.service).count()
        == 2
    )


def test_build_admin_context_has_name_tariff_and_end_date(session, client_row):
    subscription = session.query(Subscription).filter_by(client_id=client_row.id).one()

    context = build_admin_context(client_row, subscription)

    assert "Анна Смирнова" in context
    assert "@anna" in context
    assert "1 месяц" in context
    assert "active" in context
    assert "22.11.2026" in context


async def test_forward_to_admins_saves_mapping(session, client_row, admin, owner):
    _client, message, _ = store_client_message(
        session, TG_ID, "Помогите", tg_message_id=555, now=NOW
    )
    bot = FakeBot()

    links = await forward_to_admins(
        bot, session, client_row, message, active_admins(session), now=NOW
    )

    assert {chat_id for chat_id, _ in bot.sent} == {ADMIN_TG_ID, OWNER_TG_ID}
    assert bot.copied == []
    assert all("Помогите" in text for _, text in bot.sent)
    assert all("Тариф: 1 месяц" in text for _, text in bot.sent)
    assert len(links) == 2
    assert session.query(AdminMessageLink).count() == 2


def test_handle_admin_reply_routes_to_client(session, client_row, admin):
    _client, message, _ = store_client_message(session, TG_ID, "Вопрос", tg_message_id=555, now=NOW)
    session.add(
        AdminMessageLink(
            admin_tg_id=ADMIN_TG_ID,
            forwarded_message_id=777,
            client_id=client_row.id,
            client_message_id=message.id,
        )
    )
    session.flush()

    client_tg_id = handle_admin_reply(session, ADMIN_TG_ID, 777, "Ответ админа", now=NOW)

    assert client_tg_id == TG_ID
    reply = session.query(Message).filter_by(kind=MessageKind.admin).one()
    assert reply.text == "Ответ админа"
    assert reply.direction == MessageDirection.outbound
    assert reply.is_in_flow is False
    assert reply.client_id == client_row.id
    assert session.query(Message).filter_by(kind=MessageKind.client).count() == 1
    assert session.query(Message).filter_by(is_in_flow=True).count() == 0


def test_handle_admin_reply_without_mapping_returns_none(session, admin):
    assert handle_admin_reply(session, ADMIN_TG_ID, 999, "Ответ", now=NOW) is None
    assert session.query(Message).filter_by(kind=MessageKind.admin).count() == 0


def test_admin_word_creates_pending_request(session):
    admin_row = handle_admin_word(session, 940123, name="Новый")

    assert admin_row.status == AdminStatus.pending
    assert admin_status(session, 940123) == "pending"

    handle_admin_word(session, 940123, name="Новый")
    assert session.query(Admin).filter_by(tg_id=940123).count() == 1


def test_confirm_and_reject_admin(session):
    handle_admin_word(session, 940123, name="Новый")
    confirmed = confirm_admin(session, 940123, added_by=OWNER_TG_ID)
    assert confirmed.status == AdminStatus.active
    assert confirmed.added_by == OWNER_TG_ID
    assert admin_status(session, 940123) == "active"

    handle_admin_word(session, 940124, name="Второй")
    rejected = reject_admin(session, 940124)
    assert rejected is not None
    assert admin_status(session, 940124) is None


def test_ensure_owner_creates_and_is_idempotent(session):
    created = ensure_owner(session, OWNER_TG_ID)
    assert created is not None
    assert created.is_owner is True
    assert created.status == AdminStatus.active

    same = ensure_owner(session, OWNER_TG_ID)
    assert same.id == created.id
    assert session.query(Admin).filter_by(is_owner=True).count() == 1


def test_reject_does_not_delete_active_admin(session):
    handle_admin_word(session, 940125, name="Активный")
    confirm_admin(session, 940125)

    assert reject_admin(session, 940125) is None
    assert admin_status(session, 940125) == "active"


class FailingFirstBot(FakeBot):
    def __init__(self, fail_tg_id: int):
        super().__init__()
        self.fail_tg_id = fail_tg_id

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id == self.fail_tg_id:
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="boom")
        return await super().send_message(chat_id, text, reply_markup=reply_markup, **kwargs)


async def test_broadcast_survives_one_admin_failure(admin, owner):
    bot = FailingFirstBot(fail_tg_id=admin.tg_id)

    sent = await send_to_admins(bot, [admin, owner], "Новый администратор")

    assert sent == 1
    assert [chat_id for chat_id, _ in bot.sent] == [owner.tg_id]


async def test_unforwardable_message_still_reaches_admin_as_text(session, client_row, admin, owner):
    _client, message, _ = store_client_message(session, TG_ID, "", tg_message_id=556, now=NOW)
    bot = CopyFailsBot()

    links = await forward_to_admins(
        bot, session, client_row, message, active_admins(session), now=NOW
    )

    assert len(links) == 2
    assert all("[вложение]" in text for _, text in bot.sent)
    assert session.query(AdminMessageLink).count() == 2


async def test_forward_continues_after_one_admin_send_error(session, client_row, admin, owner):
    _client, message, _ = store_client_message(session, TG_ID, "Текст", tg_message_id=557, now=NOW)
    bot = FailingFirstBot(fail_tg_id=admin.tg_id)

    links = await forward_to_admins(
        bot, session, client_row, message, active_admins(session), now=NOW
    )

    assert [chat_id for chat_id, _ in bot.sent] == [owner.tg_id]
    assert len(links) == 1
    assert links[0].admin_tg_id == owner.tg_id


async def test_admin_hint_forbidden_does_not_crash(session):
    bot = ForbiddenBot()
    event = SimpleNamespace(
        from_user=SimpleNamespace(id=OWNER_TG_ID),
        text="hello",
        reply_to_message=None,
    )

    await OutOfFlowMiddleware()._handle_admin(event, session, bot)

    assert bot.attempts == 1
