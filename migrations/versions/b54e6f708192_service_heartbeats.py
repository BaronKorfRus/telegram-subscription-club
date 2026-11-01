"""service heartbeats

Revision ID: b54e6f708192
Revises: a43d5e6f7081
Create Date: 2026-10-26 23:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b54e6f708192"
down_revision: str | Sequence[str] | None = "a43d5e6f7081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_service_heartbeats"),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
