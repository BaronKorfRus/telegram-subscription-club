import pytest
from app.models import Tariff
from app.receipts import build


@pytest.fixture
def tariff_m1():
    return Tariff(
        code="m1", title="1 месяц", price_kopecks=99000, period_days=30, period_months=1
    )


def test_receipt_amounts_in_kopecks(tariff_m1):
    r = build(tariff_m1, email="a@b.ru", phone=None)
    assert r["Items"][0]["Amount"] == 99000 and r["Items"][0]["Price"] == 99000
    assert r["Taxation"] == "usn_income" and r["Items"][0]["Tax"] == "none"
    # Amount есть только в корне Init, в Receipt его быть не должно.
    assert "Amount" not in r


def test_receipt_full_structure(tariff_m1):
    assert build(tariff_m1, email="a@b.ru", phone=None) == {
        "Email": "a@b.ru",
        "Taxation": "usn_income",
        "Items": [
            {
                "Name": "1 месяц",
                "Price": 99000,
                "Quantity": 1,
                "Amount": 99000,
                "Tax": "none",
            }
        ],
    }


def test_receipt_phone_when_no_email(tariff_m1):
    r = build(tariff_m1, email=None, phone="+79001234567")
    assert r["Phone"] == "+79001234567"
    assert "Email" not in r


def test_receipt_uses_tariff_title_and_price():
    tariff = Tariff(
        code="m6", title="6 месяцев", price_kopecks=490000, period_days=180, period_months=6
    )
    r = build(tariff, email="a@b.ru", phone=None)
    assert r["Items"][0]["Name"] == "6 месяцев"
    assert r["Items"][0]["Amount"] == 490000
