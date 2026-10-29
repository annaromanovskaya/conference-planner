"""Keep the user's original discovery request in the durable queue."""
from alembic import op
import sqlalchemy as sa
revision = "20261029_0018"
down_revision = "20261029_0017"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("discovery_jobs", sa.Column("query", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("discovery_jobs", "query")
