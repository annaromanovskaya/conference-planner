"""Add workflow kind to durable agent runs.

Revision ID: 20261020_0012
Revises: 20261020_0011
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0012"
down_revision: str | None = "20261020_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("agent_runs")}
    if "workflow_kind" not in columns:
        with op.batch_alter_table("agent_runs") as batch:
            batch.add_column(
                sa.Column(
                    "workflow_kind",
                    sa.String(32),
                    nullable=False,
                    server_default="conference_research",
                )
            )
            batch.create_index(
                "ix_agent_runs_workflow_kind",
                ["workflow_kind"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("agent_runs")}
    if "workflow_kind" in columns:
        with op.batch_alter_table("agent_runs") as batch:
            batch.drop_index("ix_agent_runs_workflow_kind")
            batch.drop_column("workflow_kind")
