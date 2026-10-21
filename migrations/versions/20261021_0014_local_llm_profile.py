"""Rename the custom OpenAI-compatible model profile.

Revision ID: 20261021_0014
Revises: 20261020_0013
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261021_0014"
down_revision: str | None = "20261020_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename_profile(old: str, new: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table_name in ("model_selections", "model_capabilities"):
        if inspector.has_table(table_name):
            op.execute(
                sa.text(
                    f"UPDATE {table_name} SET profile_id = :new WHERE profile_id = :old"
                ).bindparams(old=old, new=new)
            )


def upgrade() -> None:
    _rename_profile("llm-hub", "llm-local")


def downgrade() -> None:
    _rename_profile("llm-local", "llm-hub")
