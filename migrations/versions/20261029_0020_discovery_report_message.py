"""Keep the Telegram search report message ID for updates."""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0020"
down_revision = "20261029_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("discovery_jobs", sa.Column("report_message_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("discovery_jobs", "report_message_id")
