"""Живой тест боевого терминала: Init 1 ₽, Charge по RebillId, Cancel.

Запускать только с явно заданными переменными окружения. Скрипт не пишет
платёж в базу клуба и не вызывает Salebot. Пароль, RebillId и Token не
печатаются.

Переменные: TBANK_TERMINAL_KEY, TBANK_PASSWORD, REBILL_ID, CUSTOMER_TG_ID,
CUSTOMER_EMAIL, CUSTOMER_PHONE, NOTIFICATION_URL.
"""

import hashlib
import os
import sys
from uuid import uuid4

import httpx

BASE_URL = "https://securepay.tinkoff.ru/v2"
TIMEOUT_SECONDS = 30.0
AMOUNT_KOPECKS = 100


def _token_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def make_token(params: dict, password: str) -> str:
    """Токен Т-Банка: SHA-256 значений корневых полей и пароля."""
    values = [
        (key, _token_value(value))
        for key, value in params.items()
        if key not in {"Token", "Password"} and not isinstance(value, (dict, list, tuple))
    ]
    values.append(("Password", password))
    raw = "".join(value for _, value in sorted(values))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def signed(payload: dict, password: str) -> dict:
    signed_payload = dict(payload)
    signed_payload["Token"] = make_token(signed_payload, password)
    return signed_payload


def call(operation: str, payload: dict, password: str) -> dict:
    response = httpx.post(
        f"{BASE_URL}/{operation}",
        json=signed(payload, password),
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code >= 500:
        raise RuntimeError(f"{operation}: HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"{operation}: некорректный ответ банка")
    return data


def print_result(step: str, data: dict) -> None:
    print(
        f"{step} Success={data.get('Success')} "
        f"Status={data.get('Status')} "
        f"ErrorCode={data.get('ErrorCode')} "
        f"PaymentId={data.get('PaymentId')} "
        f"Amount={data.get('Amount')}"
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"не задана переменная {name}")
    return value


def main() -> int:
    try:
        terminal_key = _required("TBANK_TERMINAL_KEY")
        password = _required("TBANK_PASSWORD")
        rebill_id = _required("REBILL_ID")
        customer_tg_id = _required("CUSTOMER_TG_ID")
        customer_email = _required("CUSTOMER_EMAIL")
        customer_phone = _required("CUSTOMER_PHONE")
        notification_url = _required("NOTIFICATION_URL")
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    order_id = f"livetest-{uuid4().hex[:20]}"
    receipt = {
        "Taxation": "usn_income",
        "Email": customer_email,
        "Phone": customer_phone,
        "Items": [
            {
                "Name": "Тест 1 рубль",
                "Price": AMOUNT_KOPECKS,
                "Quantity": 1,
                "Amount": AMOUNT_KOPECKS,
                "Tax": "none",
            }
        ],
    }
    init_payload = {
        "TerminalKey": terminal_key,
        "Amount": AMOUNT_KOPECKS,
        "OrderId": order_id,
        "Description": "Тест 1 рубль",
        "CustomerKey": customer_tg_id,
        "Recurrent": "N",
        "Receipt": receipt,
        "NotificationURL": notification_url,
        "OperationInitiatorType": "R",
    }

    try:
        init = call("Init", init_payload, password)
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        print(f"ERROR Init: {type(exc).__name__}")
        return 2
    print_result("INIT", init)
    if not init.get("Success"):
        return 2

    payment_id = init.get("PaymentId")
    if not payment_id:
        print("ERROR Init: нет PaymentId")
        return 3

    try:
        charge = call(
            "Charge",
            {
                "TerminalKey": terminal_key,
                "PaymentId": payment_id,
                "RebillId": rebill_id,
            },
            password,
        )
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        print(f"ERROR Charge: {type(exc).__name__}")
        return 4
    print_result("CHARGE", charge)
    if not charge.get("Success"):
        return 4

    try:
        cancel = call(
            "Cancel",
            {
                "TerminalKey": terminal_key,
                "PaymentId": payment_id,
                "Amount": AMOUNT_KOPECKS,
            },
            password,
        )
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        print(f"ERROR Cancel: {type(exc).__name__}")
        return 5
    print_result("CANCEL", cancel)
    return 0 if cancel.get("Success") else 5


if __name__ == "__main__":
    sys.exit(main())
