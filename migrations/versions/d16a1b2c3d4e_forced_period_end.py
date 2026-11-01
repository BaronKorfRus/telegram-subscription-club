"""forced period end

Revision ID: d16a1b2c3d4e
Revises: abfe0ce84a98
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d16a1b2c3d4e"
down_revision: str | Sequence[str] | None = "abfe0ce84a98"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("forced_period_end", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "forced_period_end")
