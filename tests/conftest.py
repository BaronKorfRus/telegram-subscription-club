import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

# Тесты читают боевой content/ приватного репозитория; в публичном экспорте
# остаётся только content/example, и тогда используется он.
content_dir = Path(__file__).resolve().parents[1] / "content"
if not (content_dir / "texts.yaml").exists():
    content_dir = content_dir / "example"
os.environ.setdefault("CONTENT_DIR", str(content_dir))

test_database_url = os.environ.get("TEST_DATABASE_URL")
if not test_database_url:
    raise RuntimeError(
        "TEST_DATABASE_URL is required, e.g. "
        "postgresql+psycopg://postgres:postgres@localhost:55432/club_test"
    )

# Приложение читает DATABASE_URL; тесты всегда работают с отдельной базой.
os.environ["DATABASE_URL"] = test_database_url

from app.db import SessionLocal  # noqa: E402
from app.web.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Приводит тестовую базу к head, чтобы pytest работал на чистой базе."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session():
    with SessionLocal() as db_session:
        try:
            yield db_session
        finally:
            db_session.rollback()
