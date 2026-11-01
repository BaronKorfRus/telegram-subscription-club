from sqlalchemy.orm import Session

from app.models import Client, Payment, PaymentKind
from app.services import payments

CARD_CHANGE_AMOUNT_KOPECKS = 100
CARD_CHANGE_DESCRIPTION = "Замена карты списания"


def _receipt(client: Client) -> dict:
    receipt: dict = {
        "Taxation": "usn_income",
        "Items": [
            {
                "Name": CARD_CHANGE_DESCRIPTION,
                "Price": CARD_CHANGE_AMOUNT_KOPECKS,
                "Quantity": 1,
                "Amount": CARD_CHANGE_AMOUNT_KOPECKS,
                "Tax": "none",
            }
        ],
    }
    if client.email:
        receipt["Email"] = client.email
    if client.phone:
        receipt["Phone"] = client.phone
    return receipt


def start(session: Session, client: Client) -> tuple[Payment, str]:
    """Платёж 1 ₽ с Recurrent=Y для привязки новой карты."""
    subscription = payments.open_subscription(session, client)
    if subscription is None or subscription.tariff is None:
        raise payments.PaymentError("Нет открытой подписки")

    return payments.create_payment(
        session,
        client,
        subscription.tariff,
        PaymentKind.card_change,
        subscription=subscription,
        amount_kopecks=CARD_CHANGE_AMOUNT_KOPECKS,
        description=CARD_CHANGE_DESCRIPTION,
        receipt=_receipt(client),
    )
