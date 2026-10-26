"""Add conditional HTTP document cache.

Revision ID: 20261016_0008
Revises: 20261016_0007
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from conference_planner.db import models as _models  # noqa: F401
from conference_planner.db.base import Base

revision: str = "20261016_0008"
down_revision: str | None = "20261016_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.tables["web_document_cache"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["web_document_cache"].drop(op.get_bind(), checkfirst=True)
