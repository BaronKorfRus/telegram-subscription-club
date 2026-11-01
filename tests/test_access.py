import json
import logging

import httpx
import pytest
import respx
from app import telegram
from app.config import settings
from app.services.access import AccessError, approve_join, grant, revoke

TOKEN = "123456:TEST"
CHANNEL_ID = -1009000000001
CHAT_ID = -1009000000002


@pytest.fixture(autouse=True)
def telegram_settings(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "channel_id", CHANNEL_ID)
    monkeypatch.setattr(settings, "chat_id", CHAT_ID)
    monkeypatch.setattr(settings, "telegram_proxy", None)


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{TOKEN}/{method}"


def _ok():
    return httpx.Response(200, json={"ok": True, "result": True})


@respx.mock
def test_grant_unbans_in_channel_and_chat():
    route = respx.post(_url("unbanChatMember")).mock(return_value=_ok())

    grant(123)

    assert route.call_count == 2
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert {body["chat_id"] for body in bodies} == {CHANNEL_ID, CHAT_ID}
    assert all(body["user_id"] == 123 for body in bodies)
    assert all(body["only_if_banned"] is True for body in bodies)


def test_grant_raises_when_telegram_unavailable(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise telegram.TelegramError("timed out")

    monkeypatch.setattr(telegram, "call", raise_timeout)

    with pytest.raises(AccessError):
        grant(123)


@respx.mock
def test_revoke_bans_in_channel_and_chat():
    route = respx.post(_url("banChatMember")).mock(return_value=_ok())

    revoke(123)

    assert route.call_count == 2
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert {body["chat_id"] for body in bodies} == {CHANNEL_ID, CHAT_ID}
    assert all(body["user_id"] == 123 for body in bodies)


@respx.mock
def test_approve_join_approves_in_channel():
    route = respx.post(_url("approveChatJoinRequest")).mock(return_value=_ok())

    assert approve_join(123) is True

    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body == {"chat_id": CHANNEL_ID, "user_id": 123}


@respx.mock
def test_telegram_ok_false_becomes_access_error(caplog):
    route = respx.post(_url("unbanChatMember")).mock(
        return_value=httpx.Response(
            200, json={"ok": False, "description": "Bad Request: not enough rights"}
        )
    )
    caplog.set_level(logging.ERROR, logger="app.services.access")

    with pytest.raises(AccessError, match="not enough rights"):
        grant(123)

    assert route.call_count == 1
    assert any("not enough rights" in record.message for record in caplog.records)


@respx.mock
def test_network_error_becomes_access_error():
    respx.post(_url("banChatMember")).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(AccessError):
        revoke(123)


@respx.mock
def test_server_error_becomes_access_error():
    respx.post(_url("approveChatJoinRequest")).mock(return_value=httpx.Response(502))

    with pytest.raises(AccessError):
        approve_join(123)


def test_missing_bot_token_raises_access_error(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")

    with pytest.raises(AccessError, match="BOT_TOKEN"):
        grant(123)


def test_missing_chat_config_raises_access_error(monkeypatch):
    monkeypatch.setattr(settings, "chat_id", None)

    with pytest.raises(AccessError, match="CHANNEL_ID"):
        grant(123)


@respx.mock
def test_partial_failure_stops_and_reraises():
    route = respx.post(_url("unbanChatMember")).mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": True}),
            httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot is not a member"}
            ),
        ]
    )

    with pytest.raises(AccessError):
        grant(123)

    assert route.call_count == 2
