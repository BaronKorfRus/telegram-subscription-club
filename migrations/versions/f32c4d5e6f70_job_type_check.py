"""job type check with expire_check

Revision ID: f32c4d5e6f70
Revises: e21b3c4d5e6f
Create Date: 2026-10-26 11:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f32c4d5e6f70"
down_revision: str | Sequence[str] | None = "e21b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES_WITH_EXPIRE_CHECK = (
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
)
TYPES_BEFORE = TYPES_WITH_EXPIRE_CHECK[:-1]


def _check(values: Sequence[str]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"type IN ({joined})"


def upgrade() -> None:
    # До этой миграции тип хранился как VARCHAR без ограничения; добавляем CHECK
    # со всеми актуальными значениями, включая expire_check.
    op.create_check_constraint("job_type_check", "jobs", _check(TYPES_WITH_EXPIRE_CHECK))


def downgrade() -> None:
    op.drop_constraint("job_type_check", "jobs", type_="check")
