"""Add Ollama free usage aggregates and provider state.

Revision ID: 20261014_0003
Revises: 20261013_0002
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

from conference_planner.db import models as _models  # noqa: F401
from conference_planner.db.base import Base

revision: str = "20261014_0003"
down_revision: str | None = "20261013_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ollama_usage_daily", "provider_usage_status")


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in TABLES:
        Base.metadata.tables[table_name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(TABLES):
        Base.metadata.tables[table_name].drop(bind, checkfirst=True)
