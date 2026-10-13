from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select

from conference_scout.db.base import Base
from conference_scout.db.migrations import run_migrations
from conference_scout.db.models import Topic, User

AGENT_TABLES = {
    "model_selections",
    "model_capabilities",
    "conversations",
    "chat_messages",
    "user_memories",
    "pending_actions",
    "knowledge_relations",
}


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def test_migration_upgrades_current_schema_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database}")
    legacy_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in AGENT_TABLES
    ]
    Base.metadata.create_all(engine, tables=legacy_tables)
    with engine.begin() as connection:
        result = connection.execute(User.__table__.insert().values(telegram_id=123))
        user_id = result.inserted_primary_key[0]
        connection.execute(
            Topic.__table__.insert().values(
                user_id=user_id,
                name="Astro",
                description="Stars",
                keywords=["stellar spectra"],
                enabled=True,
            )
        )
    engine.dispose()

    run_migrations(_url(database))

    migrated = create_engine(f"sqlite:///{database}")
    tables = set(inspect(migrated).get_table_names())
    with migrated.connect() as connection:
        assert connection.scalar(select(User.telegram_id)) == 123
        assert connection.scalar(select(Topic.name)) == "Astro"
        assert connection.exec_driver_sql("select version_num from alembic_version").scalar_one()
    migrated.dispose()
    assert AGENT_TABLES <= tables


def test_migration_creates_complete_fresh_schema(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"

    run_migrations(_url(database))

    engine = create_engine(f"sqlite:///{database}")
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert set(Base.metadata.tables) | {"alembic_version"} == tables


def test_migration_rejects_partial_unknown_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / "partial.db"
    engine = create_engine(f"sqlite:///{database}")
    User.__table__.create(engine)
    engine.dispose()

    with pytest.raises(RuntimeError, match="partial or unknown"):
        run_migrations(_url(database))
