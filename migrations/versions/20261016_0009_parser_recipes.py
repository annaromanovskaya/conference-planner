"""Add safe versioned site parser recipes.

Revision ID: 20261016_0009
Revises: 20261016_0008
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from conference_planner.db import models as _models  # noqa: F401
from conference_planner.db.base import Base

revision: str = "20261016_0009"
down_revision: str | None = "20261016_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.tables["site_parser_recipes"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["site_parser_recipes"].drop(op.get_bind(), checkfirst=True)
