from app import texts
from app.models import Tariff
from app.services import payments


def _tariff(title: str = "1 месяц") -> Tariff:
    return Tariff(
        code="m1", title=title, price_kopecks=99000, period_days=30, period_months=1
    )


def test_payment_description_uses_content(monkeypatch):
    monkeypatch.setattr(texts, "BRAND", "ExampleClub")
    monkeypatch.setattr(texts, "PAYMENT_DESCRIPTION", "Подписка {brand}: {title}")

    assert payments.payment_description(_tariff()) == "Подписка ExampleClub: 1 месяц"


def test_payment_description_strips_double_quotes(monkeypatch):
    monkeypatch.setattr(texts, "BRAND", "ExampleClub")
    monkeypatch.setattr(texts, "PAYMENT_DESCRIPTION", 'Подписка "{brand}"')

    assert payments.payment_description(_tariff()) == "Подписка ExampleClub"
