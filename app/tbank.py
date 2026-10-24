import hashlib
import hmac

import httpx

from app.config import settings

BASE_URL = "https://securepay.tinkoff.ru/v2"
TIMEOUT_SECONDS = 30.0


class TbankError(RuntimeError):
    """Сетевой сбой, 5xx или некорректный ответ банка."""


def _token_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def make_token(params: dict, password: str) -> str:
    """SHA-256 от значений корневых параметров и пароля, отсортированных по имени."""
    pairs = [
        (key, _token_value(value))
        for key, value in params.items()
        if key not in {"Token", "Password"} and not isinstance(value, (dict, list, tuple))
    ]
    pairs.append(("Password", password))
    raw = "".join(value for _, value in sorted(pairs))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_token(payload: dict, password: str) -> bool:
    token = payload.get("Token")
    if not isinstance(token, str) or not token:
        return False
    return hmac.compare_digest(make_token(payload, password), token.lower())


def _credentials() -> tuple[str, str]:
    if not settings.tbank_terminal_key or not settings.tbank_password:
        raise TbankError("TBANK_TERMINAL_KEY и TBANK_PASSWORD должны быть заданы")
    return settings.tbank_terminal_key, settings.tbank_password


def _post(operation: str, payload: dict) -> dict:
    try:
        response = httpx.post(f"{BASE_URL}/{operation}", json=payload, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise TbankError(f"{operation}: сетевой сбой: {exc}") from exc

    # 5xx — инфраструктурная проблема, наверх уходит исключение.
    if response.status_code >= 500:
        raise TbankError(f"{operation}: HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise TbankError(f"{operation}: некорректный ответ банка") from exc

    if not isinstance(data, dict):
        raise TbankError(f"{operation}: неожиданный формат ответа банка")
    return data


def _signed(payload: dict, password: str) -> dict:
    signed = dict(payload)
    signed["Token"] = make_token(signed, password)
    return signed


def init_payment(
    amount_kopecks: int,
    order_id: str,
    description: str,
    customer_key: str,
    recurrent: bool,
    receipt: dict,
    notification_url: str | None = None,
    extra: dict | None = None,
) -> dict:
    terminal_key, password = _credentials()
    payload: dict = {
        "TerminalKey": terminal_key,
        "Amount": amount_kopecks,
        "OrderId": order_id,
        "Description": description,
        "CustomerKey": customer_key,
        "Recurrent": "Y" if recurrent else "N",
        "Receipt": receipt,
    }
    if notification_url:
        payload["NotificationURL"] = notification_url
    if extra:
        payload.update(extra)
    return _post("Init", _signed(payload, password))


def charge(payment_id: str, rebill_id: str) -> dict:
    terminal_key, password = _credentials()
    payload = {
        "TerminalKey": terminal_key,
        "PaymentId": payment_id,
        "RebillId": rebill_id,
    }
    return _post("Charge", _signed(payload, password))


def get_state(payment_id: str) -> dict:
    terminal_key, password = _credentials()
    payload = {"TerminalKey": terminal_key, "PaymentId": payment_id}
    return _post("GetState", _signed(payload, password))


def cancel(payment_id: str, amount_kopecks: int | None = None) -> dict:
    terminal_key, password = _credentials()
    payload: dict = {"TerminalKey": terminal_key, "PaymentId": payment_id}
    if amount_kopecks is not None:
        payload["Amount"] = amount_kopecks
    return _post("Cancel", _signed(payload, password))
