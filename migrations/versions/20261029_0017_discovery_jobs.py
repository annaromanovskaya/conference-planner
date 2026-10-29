"""Durable agent discovery queue.

Revision ID: 20261029_0017
Revises: 20261029_0016
"""
import sqlalchemy as sa
from alembic import op
revision = "20261029_0017"
down_revision = "20261029_0016"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "discovery_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("years", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer()),
        sa.Column("found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id")),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("message_id", sa.Integer()),
        sa.Column("error_code", sa.String(150)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_discovery_jobs_user_id", "discovery_jobs", ["user_id"])
    op.create_index("ix_discovery_jobs_status", "discovery_jobs", ["status"])

def downgrade():
    op.drop_index("ix_discovery_jobs_status", table_name="discovery_jobs")
    op.drop_index("ix_discovery_jobs_user_id", table_name="discovery_jobs")
    op.drop_table("discovery_jobs")
