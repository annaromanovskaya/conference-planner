"""Persist URL retry eligibility to avoid immediate retries and starvation."""
import sqlalchemy as sa
from alembic import op

revision = "20261030_0022"
down_revision = "20261030_0021"
branch_labels = None
depends_on = None


def upgrade():
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("discovery_url_tasks")}
    if "next_retry_at" not in columns:
        with op.batch_alter_table("discovery_url_tasks") as batch:
            batch.add_column(sa.Column("next_retry_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("discovery_url_tasks") as batch:
        batch.drop_column("next_retry_at")
