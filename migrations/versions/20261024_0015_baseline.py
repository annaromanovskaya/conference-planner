"""Create the complete Conference Planner baseline schema.

Revision ID: 20261024_0015
Revises:
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from conference_planner.db import models as _models  # noqa: F401
from conference_planner.db.base import Base

revision: str = "20261024_0015"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names()) - {"alembic_version"}
    if existing:
        raise RuntimeError(
            "baseline cannot initialize a non-empty database without a known revision"
        )
    Base.metadata.create_all(bind, checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind(), checkfirst=True)
