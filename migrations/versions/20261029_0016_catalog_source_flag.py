"""Separate conference catalogs from actual conferences.

Revision ID: 20261029_0016
Revises: 20261024_0015
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0016"
down_revision: str | None = "20261024_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("conferences"):
        return
    columns = {item["name"] for item in inspector.get_columns("conferences")}
    if "is_catalog_source" not in columns:
        op.add_column(
            "conferences",
            sa.Column(
                "is_catalog_source",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )

    indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("conferences")
    }
    if "ix_conferences_is_catalog_source" not in indexes:
        op.create_index(
            "ix_conferences_is_catalog_source",
            "conferences",
            ["is_catalog_source"],
        )

    if inspector.has_table("trusted_sources"):
        op.execute(
            sa.text(
                """
                UPDATE conferences
                SET is_catalog_source = 1
                WHERE EXISTS (
                    SELECT 1
                    FROM trusted_sources AS source
                    WHERE
                        (
                            rtrim(lower(coalesce(conferences.website, '')), '/') =
                                rtrim(lower(source.url), '/')
                            AND lower(conferences.name) = lower(source.name)
                        )
                        OR (
                            coalesce(conferences.website, '') = ''
                            AND rtrim(lower(coalesce(conferences.source_url, '')), '/') =
                                rtrim(lower(source.url), '/')
                            AND lower(conferences.name) = lower(source.name)
                        )
                )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("conferences"):
        return
    columns = {item["name"] for item in inspector.get_columns("conferences")}
    if "is_catalog_source" in columns:
        indexes = {item["name"] for item in inspector.get_indexes("conferences")}
        if "ix_conferences_is_catalog_source" in indexes:
            op.drop_index("ix_conferences_is_catalog_source", table_name="conferences")
        op.drop_column("conferences", "is_catalog_source")
