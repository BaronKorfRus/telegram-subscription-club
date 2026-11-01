from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import content
from app.models import Tariff


def seed_tariffs(session: Session) -> None:
    """Добавляет недостающие тарифы из контента; существующие строки не трогает."""
    existing = set(session.scalars(select(Tariff.code)))
    for data in content.load_tariffs():
        if data.code not in existing:
            session.add(Tariff(**asdict(data)))
    session.flush()
