"""Add durable agent workflow runs, tasks, and artifacts.

Revision ID: 20261016_0006
Revises: 20261015_0005
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from conference_scout.db import models as _models  # noqa: F401
from conference_scout.db.base import Base

revision: str = "20261016_0006"
down_revision: str | None = "20261015_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("agent_runs", "agent_tasks", "agent_artifacts")


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in TABLES:
        Base.metadata.tables[table_name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(TABLES):
        Base.metadata.tables[table_name].drop(bind, checkfirst=True)
