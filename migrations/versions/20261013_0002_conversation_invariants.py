"""Enforce one active conversation per owner.

Revision ID: 20261013_0002
Revises: 20261013_0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0002"
down_revision: str | None = "20261013_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE conversations SET status = 'CLOSED' "
            "WHERE status = 'ACTIVE' AND id NOT IN "
            "(SELECT MAX(id) FROM conversations WHERE status = 'ACTIVE' GROUP BY user_id)"
        )
    )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("conversations")}
    if "uq_conversations_active_user" not in indexes:
        op.create_index(
            "uq_conversations_active_user",
            "conversations",
            ["user_id"],
            unique=True,
            sqlite_where=sa.text("status = 'ACTIVE'"),
        )


def downgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("conversations")}
    if "uq_conversations_active_user" in indexes:
        op.drop_index("uq_conversations_active_user", table_name="conversations")
