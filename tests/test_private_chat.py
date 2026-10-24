from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, ChatJoinRequest, Message, Update, User
from app import texts
from app.bot.handlers import join
from app.bot.keyboards import MENU_LABELS, main_reply_keyboard
from app.bot.main import create_dispatcher
from app.db import SessionLocal
from app.models import (
    Admin,
    AdminMessageLink,
    AdminStatus,
    Client,
    MessageKind,
)
from app.models import (
    Message as DbMessage,
)
from app.services.messages import SERVICE_REPLY_TEXT
from sqlalchemy import delete, select

GROUP_ANON_ID = 1087968824
GROUP_USER_ID = 950002
CLIENT_TG_ID = 950001
OWNER_TG_ID = 950099
CHANNEL_ID = -1009500000001
SUPERGROUP_ID = -1009500000002

TEST_TG_IDS = {
    GROUP_ANON_ID,
    GROUP_USER_ID,
    CLIENT_TG_ID,
    OWNER_TG_ID,
    *range(960001, 960021),
}


class RecordingBot:
    id = 1

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list[object] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=900 + len(self.sent))

    async def copy_message(self, chat_id, from_chat_id, message_id):
        return SimpleNamespace(message_id=800 + len(self.sent))

    async def __call__(self, method):
        chat_id = getattr(method, "chat_id", None)
        text = getattr(method, "text", "")
        self.sent.append((chat_id, text))
        self.markups.append(getattr(method, "reply_markup", None))
        return SimpleNamespace(message_id=900 + len(self.sent))


def _clean_rows() -> None:
    with SessionLocal() as session:
        client_ids = list(session.scalars(select(Client.id).where(Client.tg_id.in_(TEST_TG_IDS))))
        if client_ids:
            session.execute(
                delete(AdminMessageLink).where(AdminMessageLink.client_id.in_(client_ids))
            )
            session.execute(delete(DbMessage).where(DbMessage.client_id.in_(client_ids)))
            session.execute(delete(Client).where(Client.id.in_(client_ids)))
        session.execute(
            delete(AdminMessageLink).where(AdminMessageLink.admin_tg_id.in_(TEST_TG_IDS))
        )
        session.execute(delete(Admin).where(Admin.tg_id.in_(TEST_TG_IDS)))
        session.commit()


@pytest.fixture(autouse=True)
def clean_private_chat_rows():
    _clean_rows()
    yield
    _clean_rows()


@pytest.fixture(scope="module")
def dispatcher():
    return create_dispatcher()


@pytest.fixture
def owner() -> None:
    with SessionLocal() as session:
        session.add(
            Admin(tg_id=OWNER_TG_ID, name="Владелец", is_owner=True, status=AdminStatus.active)
        )
        session.commit()


def _message_update(
    *,
    chat_id: int,
    chat_type: ChatType,
    user_id: int,
    text: str,
    is_bot: bool = False,
    message_id: int = 1,
) -> Update:
    chat = Chat(id=chat_id, type=chat_type, title="Тестовый чат")
    user = User(id=user_id, is_bot=is_bot, first_name="Тест", username="test")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=message_id, message=message)


@pytest.mark.parametrize(
    ("chat_id", "chat_type", "user_id", "is_bot"),
    [
        (SUPERGROUP_ID, ChatType.SUPERGROUP, GROUP_ANON_ID, True),
        (SUPERGROUP_ID, ChatType.GROUP, GROUP_USER_ID, False),
        (SUPERGROUP_ID, ChatType.CHANNEL, GROUP_USER_ID, False),
    ],
)
async def test_non_private_message_is_ignored(
    owner, dispatcher, chat_id, chat_type, user_id, is_bot
):
    bot = RecordingBot()
    update = _message_update(
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        text="Привет",
        is_bot=is_bot,
    )

    await dispatcher.feed_update(bot, update)

    with SessionLocal() as session:
        client = session.scalar(select(Client).where(Client.tg_id == GROUP_ANON_ID))
        assert client is None
        messages = list(
            session.scalars(select(DbMessage).join(Client).where(Client.tg_id == GROUP_ANON_ID))
        )
        assert messages == []
        assert session.scalar(select(Admin).where(Admin.tg_id == GROUP_ANON_ID)) is None
    assert bot.sent == []


@pytest.mark.parametrize("text", ["/start", "admin"])
async def test_group_commands_and_admin_word_are_ignored(owner, dispatcher, text):
    bot = RecordingBot()
    update = _message_update(
        chat_id=SUPERGROUP_ID,
        chat_type=ChatType.SUPERGROUP,
        user_id=GROUP_USER_ID,
        text=text,
    )

    await dispatcher.feed_update(bot, update)

    with SessionLocal() as session:
        assert session.scalar(select(Client).where(Client.tg_id == GROUP_USER_ID)) is None
        assert session.scalar(select(Admin).where(Admin.tg_id == GROUP_USER_ID)) is None
    assert bot.sent == []


async def test_private_message_still_starts_client_flow(owner, dispatcher):
    bot = RecordingBot()
    update = _message_update(
        chat_id=CLIENT_TG_ID,
        chat_type=ChatType.PRIVATE,
        user_id=CLIENT_TG_ID,
        text="Здравствуйте",
    )

    await dispatcher.feed_update(bot, update)

    with SessionLocal() as session:
        client = session.scalar(select(Client).where(Client.tg_id == CLIENT_TG_ID))
        assert client is not None
        messages = list(session.scalars(select(DbMessage).where(DbMessage.client_id == client.id)))
        assert any(
            message.direction.value == "in"
            and message.kind == MessageKind.client
            and message.text == "Здравствуйте"
            for message in messages
        )
        assert any(
            message.direction.value == "out"
            and message.kind == MessageKind.service
            and message.text == SERVICE_REPLY_TEXT
            for message in messages
        )
        assert any(
            link.admin_tg_id == OWNER_TG_ID and link.client_id == client.id
            for link in session.scalars(select(AdminMessageLink))
        )

    assert any(chat_id == OWNER_TG_ID for chat_id, _ in bot.sent)
    assert (CLIENT_TG_ID, SERVICE_REPLY_TEXT) in bot.sent


async def test_chat_join_request_is_handled(monkeypatch, dispatcher):
    approved: list[int] = []

    def fake_handle(tg_id: int) -> bool:
        approved.append(tg_id)
        return True

    monkeypatch.setattr(join, "_handle_join_in_thread", fake_handle)
    monkeypatch.setattr(join.settings, "channel_id", CHANNEL_ID)
    bot = RecordingBot()
    request = ChatJoinRequest(
        chat=Chat(id=CHANNEL_ID, type=ChatType.CHANNEL, title="Канал"),
        from_user=User(id=CLIENT_TG_ID, is_bot=False, first_name="Клиент"),
        user_chat_id=CLIENT_TG_ID,
        date=datetime.now(UTC),
    )

    await dispatcher.feed_update(bot, Update(update_id=1, chat_join_request=request))

    assert approved == [CLIENT_TG_ID]
    assert bot.sent == []


def test_main_reply_keyboard_is_persistent_and_complete():
    keyboard = main_reply_keyboard()

    assert keyboard.resize_keyboard is True
    assert keyboard.is_persistent is True
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert labels == [
        "Моя подписка",
        "Оформить подписку",
        "Вопросы",
        "Правила",
        "Поддержка",
        "Описание",
    ]
    assert set(labels) == set(MENU_LABELS)


@pytest.mark.parametrize(
    ("label", "expected", "user_id"),
    [
        ("Моя подписка", texts.SUBSCRIPTION_INACTIVE, 960001),
        ("Оформить подписку", texts.ASK_NAME, 960002),
        ("Вопросы", texts.FAQ, 960003),
        ("Правила", texts.RULES, 960004),
        ("Поддержка", texts.SUPPORT, 960005),
        ("Описание", texts.DESCRIPTION, 960006),
    ],
)
async def test_menu_button_opens_section(dispatcher, label, expected, user_id):
    bot = RecordingBot()
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/start",
        ),
    )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text=label,
            message_id=2,
        ),
    )

    assert expected in bot.sent[-1][1]


@pytest.mark.parametrize(
    ("setup", "menu_label", "expected", "user_id"),
    [
        (["Оформить подписку"], "Правила", texts.RULES, 960011),
        (["Оформить подписку", "Анна"], "Вопросы", texts.FAQ, 960012),
        (
            ["Оформить подписку", "Анна", "anna@example.com"],
            "Моя подписка",
            texts.SUBSCRIPTION_INACTIVE,
            960013,
        ),
    ],
)
async def test_menu_button_during_registration_clears_step(
    dispatcher, setup, menu_label, expected, user_id
):
    bot = RecordingBot()
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/start",
        ),
    )
    for index, text in enumerate(setup, start=2):
        await dispatcher.feed_update(
            bot,
            _message_update(
                chat_id=user_id,
                chat_type=ChatType.PRIVATE,
                user_id=user_id,
                text=text,
                message_id=index,
            ),
        )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text=menu_label,
            message_id=10,
        ),
    )

    assert expected in bot.sent[-1][1]
    assert texts.ASK_EMAIL not in bot.sent[-1][1]
    assert texts.INVALID_EMAIL not in bot.sent[-1][1]
    assert texts.INVALID_PHONE not in bot.sent[-1][1]


async def _start_name_step(dispatcher, bot, user_id: int) -> None:
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/start",
        ),
    )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="Оформить подписку",
            message_id=2,
        ),
    )
    assert texts.ASK_NAME in bot.sent[-1][1]


async def _fsm_state(dispatcher, bot, user_id: int):
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    return await dispatcher.storage.get_state(key)


async def test_manage_sub_during_name_is_not_saved_as_name(dispatcher):
    user_id = 960014
    bot = RecordingBot()
    await _start_name_step(dispatcher, bot, user_id)

    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/manage_sub",
            message_id=3,
        ),
    )

    assert texts.SUBSCRIPTION_INACTIVE in bot.sent[-1][1]
    assert await _fsm_state(dispatcher, bot, user_id) is None
    with SessionLocal() as session:
        assert session.scalar(select(Client).where(Client.tg_id == user_id)) is None


async def test_support_menu_during_name_is_not_saved_as_name(dispatcher):
    user_id = 960015
    bot = RecordingBot()
    await _start_name_step(dispatcher, bot, user_id)

    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="Поддержка",
            message_id=3,
        ),
    )

    assert texts.SUPPORT in bot.sent[-1][1]
    assert await _fsm_state(dispatcher, bot, user_id) is None
    with SessionLocal() as session:
        assert session.scalar(select(Client).where(Client.tg_id == user_id)) is None


async def test_out_of_flow_after_menu_still_notifies_admins(owner, dispatcher):
    user_id = 960020
    bot = RecordingBot()
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/start",
        ),
    )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="Правила",
            message_id=2,
        ),
    )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="У меня проблема",
            message_id=3,
        ),
    )

    assert any(chat_id == OWNER_TG_ID for chat_id, _ in bot.sent)
    assert (user_id, SERVICE_REPLY_TEXT) in bot.sent


async def test_support_menu_message_has_no_inline_keyboard(dispatcher):
    bot = RecordingBot()
    user_id = 960007
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="/start",
        ),
    )
    await dispatcher.feed_update(
        bot,
        _message_update(
            chat_id=user_id,
            chat_type=ChatType.PRIVATE,
            user_id=user_id,
            text="Поддержка",
            message_id=2,
        ),
    )

    assert bot.markups[-1] is None
