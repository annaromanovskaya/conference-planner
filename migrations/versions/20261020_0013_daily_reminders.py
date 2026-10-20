"""Add durable daily reminder delivery keys.

Revision ID: 20261020_0013
Revises: 20261020_0012
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0013"
down_revision: str | None = "20261020_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("reminders"):
        return
    columns = {item["name"] for item in inspector.get_columns("reminders")}
    if "notification_kind" in columns:
        return
    with op.batch_alter_table("reminders") as batch:
        batch.add_column(
            sa.Column(
                "notification_kind",
                sa.String(32),
                nullable=False,
                server_default="EXACT",
            )
        )
        batch.add_column(
            sa.Column("threshold", sa.Integer(), nullable=False, server_default="0")
        )
    op.execute(sa.text("UPDATE reminders SET threshold = days_before"))
    with op.batch_alter_table("reminders") as batch:
        batch.create_unique_constraint(
            "uq_reminders_delivery_key",
            ["user_id", "deadline_id", "notification_kind", "threshold"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("reminders"):
        return
    columns = {item["name"] for item in inspector.get_columns("reminders")}
    if "notification_kind" not in columns:
        return
    with op.batch_alter_table("reminders") as batch:
        batch.drop_constraint("uq_reminders_delivery_key", type_="unique")
        batch.drop_column("threshold")
        batch.drop_column("notification_kind")
