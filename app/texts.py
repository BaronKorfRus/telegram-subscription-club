"""Тонкая обёртка над content-слоем; тексты живут в content/texts.yaml."""

from app import content

_KEY_MAP = {
    "BRAND": "brand",
    "PAYMENT_DESCRIPTION": "payment_description",
    "START": "start",
    "DESCRIPTION": "description",
    "FAQ": "faq",
    "RULES": "rules",
    "SUPPORT": "support",
    "MENU_HINT": "menu_hint",
    "ASK_NAME": "ask_name",
    "ASK_EMAIL": "ask_email",
    "ASK_PHONE": "ask_phone",
    "INVALID_EMAIL": "invalid_email",
    "INVALID_PHONE": "invalid_phone",
    "COHORT_MONTHLY": "cohort_monthly",
    "COHORT_3M": "cohort_3m",
    "SUBSCRIPTION_INACTIVE": "subscription_inactive",
    "SUBSCRIPTION_INACTIVE_UNTIL": "subscription_inactive_until",
    "SUBSCRIPTION_ACTIVE": "subscription_active",
    "TARIFF_CHOICE": "tariff_choice",
    "CANCEL_CONFIRM": "cancel_confirm",
    "CANCEL_DONE": "cancel_done",
    "SUBSCRIPTION_ENDED": "subscription_ended",
    "CANCEL_YES": "cancel_yes",
    "CANCEL_NO": "cancel_no",
    "CARD_CHANGE": "card_change",
    "CARD_CHANGE_BUTTON": "card_change_button",
    "JOIN_NOT_PAID": "join_not_paid",
    "OUT_OF_FLOW_ACK": "out_of_flow_ack",
    "ADMIN_REPLY_HINT": "admin_reply_hint",
    "ADMIN_REQUEST_SENT": "admin_request_sent",
    "ADMIN_ALREADY": "admin_already",
    "ADMIN_REJECTED": "admin_rejected",
    "REMINDER_3D": "reminder_3d",
    "REMINDER_1D": "reminder_1d",
    "REMINDER_3D_TEMPLATE": "reminder_3d_template",
    "REMINDER_1D_TEMPLATE": "reminder_1d_template",
    "PAYMENT_SUCCESS": "payment_success",
    "PAYMENT_SUCCESS_BUTTON": "payment_success_button",
    "CHARGE_FAILED_2H": "charge_failed_2h",
    "CHARGE_FAILED_24H": "charge_failed_24h",
    "PAYMENT_FAILED": "payment_failed",
    "SUBSCRIPTION_EXPIRED": "subscription_expired",
}
__all__ = tuple(_KEY_MAP)


def __getattr__(name: str):
    key = _KEY_MAP.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return content.get(key)
