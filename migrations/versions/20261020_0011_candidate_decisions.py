"""Add durable candidate decisions.

Revision ID: 20261020_0011
Revises: 20261019_0010
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0011"
down_revision: str | None = "20261019_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {
    "decision_reason": sa.Column("decision_reason", sa.String(32), nullable=True),
    "decision_source": sa.Column("decision_source", sa.String(16), nullable=True),
    "decision_at": sa.Column("decision_at", sa.DateTime(), nullable=True),
    "next_review_at": sa.Column("next_review_at", sa.DateTime(), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns("event_candidates")}
    missing = [column for name, column in _COLUMNS.items() if name not in existing]
    if missing:
        with op.batch_alter_table("event_candidates") as batch:
            for column in missing:
                batch.add_column(column)

    op.execute(
        sa.text(
            "UPDATE event_candidates "
            "SET decision_reason = 'LEGACY_SKIPPED', "
            "decision_source = 'SYSTEM', decision_at = updated_at, next_review_at = NULL "
            "WHERE status = 'SKIPPED' AND decision_reason IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(sa.text("UPDATE event_candidates SET status = 'SKIPPED' WHERE status = 'REJECTED'"))
    existing = {item["name"] for item in sa.inspect(bind).get_columns("event_candidates")}
    removable = [name for name in reversed(_COLUMNS) if name in existing]
    if removable:
        with op.batch_alter_table("event_candidates") as batch:
            for name in removable:
                batch.drop_column(name)
