import json

import httpx
import pytest
import respx
from app import tbank
from app.config import settings

TERMINAL_KEY = "TestTerminal"
PASSWORD = "test-password"

RECEIPT = {
    "Email": "a@b.ru",
    "Taxation": "usn_income",
    "Items": [{"Name": "1 месяц", "Price": 99000, "Quantity": 1, "Amount": 99000, "Tax": "none"}],
}


@pytest.fixture(autouse=True)
def tbank_credentials(monkeypatch):
    monkeypatch.setattr(settings, "tbank_terminal_key", TERMINAL_KEY)
    monkeypatch.setattr(settings, "tbank_password", PASSWORD)


def test_token_matches_docs_vector():
    # В опубликованном примере T-Bank OrderId = "00000" (в теле примера
    # ошибочно показан "21090"); хеш соответствует именно "00000".
    params = {
        "TerminalKey": "MerchantTerminalKey",
        "Amount": 19200,
        "OrderId": "00000",
        "Description": "Подарочная карта на 1000 рублей",
    }
    assert (
        tbank.make_token(params, "11111111111111")
        == "72dd466f8ace0a37a1f740ce5fb78101712bc0665d91a8108c7c8a0ccd426db2"
    )


def test_notification_token_matches_docs_vector():
    # Пример токена HTTP-уведомления из документации T-Bank; Success — bool.
    payload = {
        "TerminalKey": "1234567890DEMO",
        "OrderId": "000000",
        "Success": True,
        "Status": "AUTHORIZED",
        "PaymentId": "0000000",
        "ErrorCode": "0",
        "Amount": "1111",
        "CardId": "000000",
        "Pan": "200000******0000",
        "ExpDate": "1111",
        "RebillId": "000000",
    }
    token = tbank.make_token(payload, "11111111111")
    assert token == "1c0964277d0213349243065a0d5b838b8e90d2d25f740d0f2767836e710e80c8"
    assert tbank.check_token({**payload, "Token": token}, "11111111111") is True


@respx.mock
def test_init_mixes_extra_into_payload():
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(
        return_value=httpx.Response(200, json={"Success": True, "PaymentId": "1"})
    )

    tbank.init_payment(
        amount_kopecks=99000,
        order_id="order-extra",
        description="Продление",
        customer_key="42",
        recurrent=False,
        receipt=RECEIPT,
        notification_url=None,
        extra={"OperationInitiatorType": "R"},
    )

    body = json.loads(route.calls.last.request.content)
    assert body["Recurrent"] == "N"
    assert body["OperationInitiatorType"] == "R"
    assert body["Token"] == tbank.make_token(body, PASSWORD)


def test_token_sorts_keys_and_includes_password():
    params = {"TerminalKey": "T", "Amount": 100, "OrderId": "o-1"}
    # Ключи сортируются: Amount, OrderId, Password, TerminalKey.
    assert tbank.make_token(params, PASSWORD) != tbank.make_token(
        {"TerminalKey": "T", "Amount": 100, "OrderId": "o-2"}, PASSWORD
    )


def test_token_ignores_nested_objects():
    base = {"TerminalKey": "T", "Amount": 100}
    with_receipt = {**base, "Receipt": {"Items": [{"Name": "x"}]}, "DATA": {"Email": "a@b.ru"}}
    assert tbank.make_token(base, PASSWORD) == tbank.make_token(with_receipt, PASSWORD)


def test_token_treats_bools_as_json_lowercase():
    assert tbank.make_token({"TerminalKey": "T", "Success": True}, PASSWORD) == tbank.make_token(
        {"TerminalKey": "T", "Success": "true"}, PASSWORD
    )


def test_check_token_accepts_valid_and_rejects_tampered():
    payload = {"TerminalKey": "T", "Amount": 100}
    payload["Token"] = tbank.make_token(payload, PASSWORD)
    assert tbank.check_token(payload, PASSWORD) is True

    tampered = {**payload, "Amount": 101}
    assert tbank.check_token(tampered, PASSWORD) is False
    assert tbank.check_token({"TerminalKey": "T"}, PASSWORD) is False


@respx.mock
def test_init_payment_payload():
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(
        return_value=httpx.Response(
            200,
            json={
                "Success": True,
                "PaymentId": "123456",
                "PaymentURL": "https://pay.example/1",
            },
        )
    )

    result = tbank.init_payment(
        amount_kopecks=99000,
        order_id="order-1",
        description="Оплата подписки, 1 месяц",
        customer_key="42",
        recurrent=True,
        receipt=RECEIPT,
        notification_url="https://callback.example/tbank/notify",
    )

    assert result["Success"] is True
    assert result["PaymentId"] == "123456"
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["TerminalKey"] == TERMINAL_KEY
    assert body["Amount"] == 99000
    assert body["OrderId"] == "order-1"
    assert body["Description"] == "Оплата подписки, 1 месяц"
    assert body["CustomerKey"] == "42"
    assert body["Recurrent"] == "Y"
    assert body["Receipt"] == RECEIPT
    assert body["NotificationURL"] == "https://callback.example/tbank/notify"
    assert body["Token"] == tbank.make_token(body, PASSWORD)


@respx.mock
def test_init_payment_without_notification_url_and_recurrent():
    route = respx.post(f"{tbank.BASE_URL}/Init").mock(
        return_value=httpx.Response(200, json={"Success": True, "PaymentId": "1"})
    )

    tbank.init_payment(
        amount_kopecks=100,
        order_id="order-2",
        description="Замена карты",
        customer_key="42",
        recurrent=False,
        receipt=RECEIPT,
        notification_url=None,
    )

    body = json.loads(route.calls.last.request.content)
    assert "NotificationURL" not in body
    assert body["Recurrent"] == "N"


@respx.mock
def test_charge_payload_has_rebill_id_and_no_customer_key():
    route = respx.post(f"{tbank.BASE_URL}/Charge").mock(
        return_value=httpx.Response(200, json={"Success": True, "Status": "CONFIRMED"})
    )

    result = tbank.charge(payment_id="98765", rebill_id="rebill-1")

    assert result["Status"] == "CONFIRMED"
    body = json.loads(route.calls.last.request.content)
    assert body["TerminalKey"] == TERMINAL_KEY
    assert body["PaymentId"] == "98765"
    assert body["RebillId"] == "rebill-1"
    assert "CustomerKey" not in body
    assert body["Token"] == tbank.make_token(body, PASSWORD)


@respx.mock
def test_get_state_payload():
    route = respx.post(f"{tbank.BASE_URL}/GetState").mock(
        return_value=httpx.Response(200, json={"Success": True, "Status": "CONFIRMED"})
    )

    assert tbank.get_state("98765")["Status"] == "CONFIRMED"
    body = json.loads(route.calls.last.request.content)
    assert body["PaymentId"] == "98765"
    assert body["Token"] == tbank.make_token(body, PASSWORD)


@respx.mock
def test_cancel_with_and_without_amount():
    route = respx.post(f"{tbank.BASE_URL}/Cancel").mock(
        return_value=httpx.Response(200, json={"Success": True})
    )

    tbank.cancel("1")
    assert "Amount" not in json.loads(route.calls.last.request.content)

    tbank.cancel("1", amount_kopecks=100)
    body = json.loads(route.calls.last.request.content)
    assert body["Amount"] == 100
    assert body["Token"] == tbank.make_token(body, PASSWORD)


@respx.mock
def test_bank_business_error_is_returned_as_dict():
    error = {"Success": False, "ErrorCode": "7", "Message": "Insufficient funds"}
    respx.post(f"{tbank.BASE_URL}/Charge").mock(return_value=httpx.Response(200, json=error))

    assert tbank.charge("1", "r") == error


@respx.mock
def test_client_error_with_json_is_returned_as_dict():
    error = {"Success": False, "ErrorCode": "204", "Message": "Bad request"}
    respx.post(f"{tbank.BASE_URL}/Init").mock(return_value=httpx.Response(400, json=error))

    assert tbank.init_payment(100, "o", "d", "c", True, RECEIPT, None) == error


@respx.mock
def test_server_error_raises():
    respx.post(f"{tbank.BASE_URL}/Charge").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(tbank.TbankError):
        tbank.charge("1", "r")


@respx.mock
def test_network_error_raises():
    respx.post(f"{tbank.BASE_URL}/Charge").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(tbank.TbankError):
        tbank.charge("1", "r")
