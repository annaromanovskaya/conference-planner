"""Keep the Telegram search report message ID for updates."""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0020"
down_revision = "20261029_0019"
branch_labels = None
depends_on = None


def upgrade():
    if "report_message_id" in {col["name"] for col in sa.inspect(op.get_bind()).get_columns("discovery_jobs")}:
        return
    op.add_column("discovery_jobs", sa.Column("report_message_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("discovery_jobs", "report_message_id")
