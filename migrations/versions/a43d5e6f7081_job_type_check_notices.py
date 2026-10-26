"""job type check with window notices

Revision ID: a43d5e6f7081
Revises: f32c4d5e6f70
Create Date: 2026-10-26 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a43d5e6f7081"
down_revision: str | Sequence[str] | None = "f32c4d5e6f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "job_type_check"
TYPES = (
    "reminder_3d",
    "reminder_1d",
    "charge",
    "expire",
    "kick",
    "reconcile",
    "post_payment",
    "payment_failed",
    "daily_report",
    "expire_check",
    "window_soon",
    "window_open",
)
TYPES_BEFORE = TYPES[:-2]


def _check(values: Sequence[str]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"type IN ({joined})"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "jobs", type_="check")
    op.create_check_constraint(CONSTRAINT_NAME, "jobs", _check(TYPES))


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "jobs", type_="check")
    op.create_check_constraint(CONSTRAINT_NAME, "jobs", _check(TYPES_BEFORE))
