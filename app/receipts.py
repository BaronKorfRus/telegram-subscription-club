from app.models import Tariff


def build(tariff: Tariff, email: str | None, phone: str | None) -> dict:
    """Чек для 54-ФЗ: одна позиция — тариф, все суммы в копейках."""
    receipt: dict = {
        "Taxation": "usn_income",
        "Items": [
            {
                "Name": tariff.title,
                "Price": tariff.price_kopecks,
                "Quantity": 1,
                "Amount": tariff.price_kopecks,
                "Tax": "none",
            }
        ],
    }
    if email:
        receipt["Email"] = email
    if phone:
        receipt["Phone"] = phone
    return receipt
