"""Persistent URL crawl queue and lease recovery."""
import sqlalchemy as sa
from alembic import op

revision = "20261030_0021"
down_revision = "20261029_0020"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("discovery_url_tasks"):
        return
    op.create_table(
        "discovery_url_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("discovery_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("normalized_url", sa.String(2048), nullable=False),
        sa.Column("parent_url", sa.String(2048)),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("event_candidates.id", ondelete="SET NULL")),
        sa.Column("root_url", sa.String(2048), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("task_type", sa.String(30), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "normalized_url", "task_type", name="uq_discovery_url_task"),
    )
    op.create_index("ix_discovery_url_tasks_job_id", "discovery_url_tasks", ["job_id"])
    op.create_index("ix_discovery_url_tasks_status", "discovery_url_tasks", ["status"])


def downgrade():
    op.drop_index("ix_discovery_url_tasks_status")
    op.drop_index("ix_discovery_url_tasks_job_id")
    op.drop_table("discovery_url_tasks")
