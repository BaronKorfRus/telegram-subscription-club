import os

import pytest
from app import content
from app.content import ContentError
from app.models import Tariff
from app.seed import seed_tariffs

VALID_TARIFF = """
- code: m1
  title: 1 месяц
  price_kopecks: 99000
  period_days: 30
  is_legacy: false
  is_active: true
  sort_order: 1
"""


@pytest.fixture(autouse=True)
def restore_content(monkeypatch):
    original = os.environ.get("CONTENT_DIR")
    yield
    if original is None:
        monkeypatch.delenv("CONTENT_DIR", raising=False)
    else:
        monkeypatch.setenv("CONTENT_DIR", original)
    content.reload()


def _write(tmp_path, monkeypatch, text: str) -> None:
    (tmp_path / "tariffs.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path))
    content.reload()


def test_tariffs_from_yaml(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, VALID_TARIFF)

    rows = content.load_tariffs()

    assert rows[0].code == "m1" and rows[0].price_kopecks == 99000


def test_duplicate_code_is_rejected(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, VALID_TARIFF + VALID_TARIFF)

    with pytest.raises(ContentError, match="m1"):
        content.load_tariffs()


def test_non_positive_price_is_rejected(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, VALID_TARIFF.replace("99000", "0"))

    with pytest.raises(ContentError, match="price_kopecks"):
        content.load_tariffs()


def test_unsupported_period_is_rejected(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, VALID_TARIFF.replace("period_days: 30", "period_days: 45"))

    with pytest.raises(ContentError, match="period_days"):
        content.load_tariffs()


def test_seed_uses_tariffs_from_content(tmp_path, monkeypatch, session):
    _write(tmp_path, monkeypatch, VALID_TARIFF.replace("m1", "custom").replace("99000", "12345"))

    seed_tariffs(session)

    row = session.query(Tariff).filter_by(code="custom").one()
    assert row.price_kopecks == 12345
    assert row.period_days == 30
