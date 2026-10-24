import os

import pytest
import yaml
from app import content
from app.content import MAPPING_KEYS, REQUIRED_KEYS, ContentError


def _valid_data() -> dict:
    data = {key: f"text-{key}" for key in REQUIRED_KEYS if key not in MAPPING_KEYS}
    for key in MAPPING_KEYS:
        data[key] = {99000: f"reminder-{key}"}
    return data


def _write(tmp_path, data: dict) -> None:
    (tmp_path / "texts.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def restore_content(monkeypatch):
    original = os.environ.get("CONTENT_DIR")
    yield
    if original is None:
        monkeypatch.delenv("CONTENT_DIR", raising=False)
    else:
        monkeypatch.setenv("CONTENT_DIR", original)
    content.reload()


def test_texts_loaded_from_temp(tmp_path, monkeypatch):
    data = _valid_data()
    data["start"] = "Привет"
    _write(tmp_path, data)
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path))
    content.reload()

    assert content.get("start") == "Привет"


def test_missing_required_key_is_reported(tmp_path, monkeypatch):
    data = _valid_data()
    data.pop("start")
    _write(tmp_path, data)
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path))
    content.reload()

    with pytest.raises(ContentError, match="start"):
        content.load_texts()


def test_unknown_key_is_reported(tmp_path, monkeypatch):
    _write(tmp_path, _valid_data())
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path))
    content.reload()

    with pytest.raises(ContentError, match="nope"):
        content.get("nope")
