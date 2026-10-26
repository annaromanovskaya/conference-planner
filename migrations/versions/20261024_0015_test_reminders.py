"""Add persistent administrative test reminders.

Revision ID: 20261024_0015
Revises: 20261021_0014
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0015"
down_revision: str | None = "20261021_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conference_columns = (
        {item["name"] for item in inspector.get_columns("conferences")}
        if inspector.has_table("conferences")
        else set()
    )
    if inspector.has_table("conferences") and "is_test" not in conference_columns:
        op.add_column(
            "conferences",
            sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if not inspector.has_table("test_reminder_tasks"):
        op.create_table(
            "test_reminder_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("conference_id", sa.Integer(), nullable=False),
            sa.Column("deadline_id", sa.Integer(), nullable=False),
            sa.Column("send_at", sa.DateTime(), nullable=False),
            sa.Column(
                "status",
                sa.Enum("PENDING", "FAILED", name="testreminderstatus"),
                nullable=False,
            ),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["conference_id"], ["conferences.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["deadline_id"], ["deadlines.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("user_id"),
            sa.UniqueConstraint("conference_id"),
            sa.UniqueConstraint("deadline_id"),
        )
        op.create_index("ix_test_reminder_tasks_user_id", "test_reminder_tasks", ["user_id"])
        op.create_index("ix_test_reminder_tasks_send_at", "test_reminder_tasks", ["send_at"])
        op.create_index("ix_test_reminder_tasks_status", "test_reminder_tasks", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("test_reminder_tasks"):
        op.drop_table("test_reminder_tasks")
    conference_columns = (
        {item["name"] for item in inspector.get_columns("conferences")}
        if inspector.has_table("conferences")
        else set()
    )
    if "is_test" in conference_columns:
        op.drop_column("conferences", "is_test")
