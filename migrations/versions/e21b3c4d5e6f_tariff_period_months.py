"""tariff period months

Revision ID: e21b3c4d5e6f
Revises: d18a1b2c3d4f
Create Date: 2026-10-26 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e21b3c4d5e6f"
down_revision: str | Sequence[str] | None = "d18a1b2c3d4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Длина периода в месяцах выводится из исторической period_days.
MONTHS_BY_DAYS = {30: 1, 90: 3, 180: 6}


def upgrade() -> None:
    op.add_column("tariffs", sa.Column("period_months", sa.Integer(), nullable=True))
    for days, months in MONTHS_BY_DAYS.items():
        statement = sa.text(
            "UPDATE tariffs SET period_months = :months WHERE period_days = :days"
        ).bindparams(months=months, days=days)
        op.execute(statement)
    # Всё, что не попало в известные сроки, считаем месячным: иначе NOT NULL упадёт.
    op.execute(sa.text("UPDATE tariffs SET period_months = 1 WHERE period_months IS NULL"))
    op.alter_column("tariffs", "period_months", nullable=False)


def downgrade() -> None:
    op.drop_column("tariffs", "period_months")
