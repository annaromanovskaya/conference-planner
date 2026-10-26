"""Add the conversational agent infrastructure.

Revision ID: 20261013_0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from conference_planner.db import models as _models  # noqa: F401
from conference_planner.db.base import Base

revision: str = "20261013_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TABLES = {
    "users",
    "topics",
    "trusted_sources",
    "conferences",
    "conference_topics",
    "deadlines",
    "subscriptions",
    "reminders",
    "deadline_history",
    "conference_feedback",
}
AGENT_TABLES = (
    "model_selections",
    "model_capabilities",
    "conversations",
    "chat_messages",
    "user_memories",
    "pending_actions",
    "knowledge_relations",
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names()) - {"alembic_version"}
    if not existing:
        Base.metadata.create_all(bind)
        return
    allowed = LEGACY_TABLES | set(AGENT_TABLES)
    if not LEGACY_TABLES.issubset(existing) or not existing.issubset(allowed):
        raise RuntimeError("database has a partial or unknown legacy schema")
    for table_name in AGENT_TABLES:
        Base.metadata.tables[table_name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(AGENT_TABLES):
        Base.metadata.tables[table_name].drop(bind, checkfirst=True)
