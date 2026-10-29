"""Persist SearXNG query and result diagnostics per discovery job."""
from alembic import op
import sqlalchemy as sa

revision = "20261029_0019"
down_revision = "20261029_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_search_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("discovery_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("retained_count", sa.Integer(), nullable=False),
        sa.Column("ignored_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("urls", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_discovery_search_audit_job_id", "discovery_search_audit", ["job_id"])


def downgrade():
    op.drop_index("ix_discovery_search_audit_job_id", table_name="discovery_search_audit")
    op.drop_table("discovery_search_audit")
