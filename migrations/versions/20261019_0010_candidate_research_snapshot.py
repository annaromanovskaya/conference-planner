"""Add durable candidate research snapshots.

Revision ID: 20261019_0010
Revises: 20261016_0009
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261019_0010"
down_revision: str | None = "20261016_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {
    "start_date": sa.Column("start_date", sa.Date(), nullable=True),
    "end_date": sa.Column("end_date", sa.Date(), nullable=True),
    "location": sa.Column("location", sa.String(500), nullable=True),
    "official_url": sa.Column("official_url", sa.String(2048), nullable=True),
    "research_payload": sa.Column("research_payload", sa.JSON(), nullable=True),
    "researched_at": sa.Column("researched_at", sa.DateTime(), nullable=True),
    "topic_relevance_score": sa.Column("topic_relevance_score", sa.Float(), nullable=True),
    "topic_relevance_reason": sa.Column("topic_relevance_reason", sa.Text(), nullable=True),
}


def upgrade() -> None:
    existing = {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("event_candidates")
    }
    missing = [column for name, column in _COLUMNS.items() if name not in existing]
    if missing:
        with op.batch_alter_table("event_candidates") as batch:
            for column in missing:
                batch.add_column(column)


def downgrade() -> None:
    existing = {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("event_candidates")
    }
    removable = [name for name in reversed(_COLUMNS) if name in existing]
    if removable:
        with op.batch_alter_table("event_candidates") as batch:
            for name in removable:
                batch.drop_column(name)
