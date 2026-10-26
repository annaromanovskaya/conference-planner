"""Allow several conference candidates to share one catalog URL.

Revision ID: 20261015_0005
Revises: 20261014_0004
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261015_0005"
down_revision: str | None = "20261014_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _unique_constraint(columns: tuple[str, ...]) -> str | None:
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("event_candidates"):
        if tuple(constraint["column_names"]) == columns:
            return constraint["name"] or f"uq_event_candidates_{columns[0]}"
    return None


def upgrade() -> None:
    old_constraint = _unique_constraint(("user_id", "source_url"))
    if old_constraint is None:
        # Fresh databases may have been created by revision 0004 from current
        # metadata and therefore already contain the new constraint.
        if _unique_constraint(("user_id", "source_url", "normalized_title")):
            return
        raise RuntimeError("event_candidates has no recognized unique constraint")
    with op.batch_alter_table(
        "event_candidates",
        recreate="always",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(
            old_constraint,
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_event_candidates_user_source_title",
            ["user_id", "source_url", "normalized_title"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT user_id, source_url FROM event_candidates "
            "GROUP BY user_id, source_url HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade event_candidates: duplicate catalog URLs would be lost"
        )

    new_constraint = _unique_constraint(("user_id", "source_url", "normalized_title"))
    if new_constraint is None:
        if _unique_constraint(("user_id", "source_url")):
            return
        raise RuntimeError("event_candidates has no recognized unique constraint")

    with op.batch_alter_table(
        "event_candidates",
        recreate="always",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(
            new_constraint,
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_event_candidates_user_id",
            ["user_id", "source_url"],
        )
