"""notification errors

Revision ID: d18a1b2c3d4f
Revises: d16a1b2c3d4e
Create Date: 2026-10-24 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d18a1b2c3d4f"
down_revision: str | Sequence[str] | None = "d16a1b2c3d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_errors")),
    )
    op.create_index(
        op.f("ix_notification_errors_tg_id"),
        "notification_errors",
        ["tg_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_errors_created_at"),
        "notification_errors",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notification_errors_created_at"), table_name="notification_errors")
    op.drop_index(op.f("ix_notification_errors_tg_id"), table_name="notification_errors")
    op.drop_table("notification_errors")
