"""Add durable safe workflow progress events.

Revision ID: 20261016_0007
Revises: 20261016_0006
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from conference_scout.db import models as _models  # noqa: F401
from conference_scout.db.base import Base

revision: str = "20261016_0007"
down_revision: str | None = "20261016_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.tables["agent_progress_events"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["agent_progress_events"].drop(op.get_bind(), checkfirst=True)
