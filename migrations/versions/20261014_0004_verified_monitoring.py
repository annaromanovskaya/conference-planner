"""Add durable verified conference monitoring data.

Revision ID: 20261014_0004
Revises: 20261014_0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from conference_scout.db import models as _models  # noqa: F401
from conference_scout.db.base import Base

revision: str = "20261014_0004"
down_revision: str | None = "20261014_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (
    "conference_aliases",
    "conference_evidence",
    "event_candidates",
    "conference_changes",
    "monitoring_attempts",
)


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("deadlines")}
    if "approximate_year" not in columns:
        with op.batch_alter_table("deadlines") as batch:
            batch.add_column(sa.Column("approximate_year", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("approximate_month", sa.Integer(), nullable=True))
            batch.add_column(
                sa.Column(
                    "tentative",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
            batch.add_column(sa.Column("verification_status", sa.String(32), nullable=True))
            batch.add_column(sa.Column("source_class", sa.String(32), nullable=True))
            batch.add_column(sa.Column("evidence_excerpt", sa.Text(), nullable=True))
            batch.add_column(sa.Column("observed_at", sa.DateTime(), nullable=True))
            batch.alter_column("deadline_date", existing_type=sa.Date(), nullable=True)

        op.execute(
            sa.text(
                "UPDATE deadlines SET verification_status = CASE WHEN verified = 1 "
                "THEN 'OFFICIAL_CONFIRMED' ELSE 'SECONDARY_UNVERIFIED' END, "
                "source_class = CASE WHEN verified = 1 THEN 'OFFICIAL_CONFERENCE' "
                "ELSE 'SECONDARY' END, observed_at = last_checked_at"
            )
        )
        with op.batch_alter_table("deadlines") as batch:
            batch.alter_column(
                "verification_status", existing_type=sa.String(32), nullable=False
            )
            batch.alter_column("source_class", existing_type=sa.String(32), nullable=False)
            batch.alter_column("observed_at", existing_type=sa.DateTime(), nullable=False)
            batch.create_check_constraint(
                "deadline_exact_or_approximate",
                "(deadline_date IS NOT NULL AND approximate_year IS NULL "
                "AND approximate_month IS NULL) OR "
                "(deadline_date IS NULL AND approximate_year IS NOT NULL)",
            )
            batch.create_check_constraint(
                "deadline_approximate_month",
                "approximate_month IS NULL OR "
                "(approximate_month >= 1 AND approximate_month <= 12)",
            )

    bind = op.get_bind()
    for table_name in NEW_TABLES:
        Base.metadata.tables[table_name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(NEW_TABLES):
        Base.metadata.tables[table_name].drop(bind, checkfirst=True)

    op.execute(sa.text("DELETE FROM deadlines WHERE deadline_date IS NULL"))
    columns = {item["name"] for item in sa.inspect(bind).get_columns("deadlines")}
    if "approximate_year" in columns:
        with op.batch_alter_table("deadlines") as batch:
            batch.drop_constraint("deadline_approximate_month", type_="check")
            batch.drop_constraint("deadline_exact_or_approximate", type_="check")
            batch.alter_column("deadline_date", existing_type=sa.Date(), nullable=False)
            batch.drop_column("observed_at")
            batch.drop_column("evidence_excerpt")
            batch.drop_column("source_class")
            batch.drop_column("verification_status")
            batch.drop_column("tentative")
            batch.drop_column("approximate_month")
            batch.drop_column("approximate_year")
