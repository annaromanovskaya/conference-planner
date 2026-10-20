from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

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
    "ollama_usage_daily",
    "provider_usage_status",
}

VERIFIED_MONITORING_TABLES = {
    "conference_aliases",
    "conference_evidence",
    "event_candidates",
    "conference_changes",
    "monitoring_attempts",
}

AGENT_WORKFLOW_TABLES = {
    "agent_runs",
    "agent_tasks",
    "agent_artifacts",
    "agent_progress_events",
    "web_document_cache",
    "site_parser_recipes",
}


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _alembic_config(database: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def test_migration_upgrades_current_schema_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database}")
    legacy_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in AGENT_TABLES | VERIFIED_MONITORING_TABLES | AGENT_WORKFLOW_TABLES
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
    inspector = inspect(migrated)
    tables = set(inspector.get_table_names())
    daily_unique_columns = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("ollama_usage_daily")
    }
    status_unique_columns = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("provider_usage_status")
    }
    with migrated.connect() as connection:
        assert connection.scalar(select(User.telegram_id)) == 123
        assert connection.scalar(select(Topic.name)) == "Astro"
        assert connection.exec_driver_sql("select version_num from alembic_version").scalar_one()
    migrated.dispose()
    assert AGENT_TABLES <= tables
    assert ("user_id", "profile_id", "model_name", "usage_date") in daily_unique_columns
    assert ("user_id", "profile_id") in status_unique_columns


def test_migration_creates_complete_fresh_schema(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"

    run_migrations(_url(database))

    engine = create_engine(f"sqlite:///{database}")
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert set(Base.metadata.tables) | {"alembic_version"} == tables
    assert AGENT_WORKFLOW_TABLES <= tables


def test_migration_rejects_partial_unknown_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / "partial.db"
    engine = create_engine(f"sqlite:///{database}")
    User.__table__.create(engine)
    engine.dispose()

    with pytest.raises(RuntimeError, match="partial or unknown"):
        run_migrations(_url(database))


def test_verified_monitoring_migration_preserves_tracked_conference(tmp_path: Path) -> None:
    database = tmp_path / "revision-0003.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('20261014_0003')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL UNIQUE, "
            "created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE conferences (id INTEGER PRIMARY KEY, name VARCHAR(500) NOT NULL, "
            "normalized_name VARCHAR(500) NOT NULL, acronym VARCHAR(100), event_type VARCHAR(100), "
            "website VARCHAR(2048) UNIQUE, source_url VARCHAR(2048), "
            "start_date DATE, end_date DATE, "
            "location VARCHAR(500), description TEXT, status VARCHAR(10) NOT NULL, "
            "last_checked_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE deadlines (id INTEGER PRIMARY KEY, conference_id INTEGER NOT NULL, "
            "deadline_type VARCHAR(32) NOT NULL, deadline_date DATE NOT NULL, "
            "source_url VARCHAR(2048) NOT NULL, verified BOOLEAN NOT NULL, "
            "last_checked_at DATETIME NOT NULL, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, UNIQUE (conference_id, deadline_type))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "conference_id INTEGER NOT NULL, status VARCHAR(10) NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "UNIQUE (user_id, conference_id))"
        )
        connection.execute(text("INSERT INTO users VALUES (1, 123, '2026-10-14 00:00:00')"))
        connection.execute(
            text(
                "INSERT INTO conferences VALUES "
                "(7, 'IMBASE 2027', 'imbase 2027', NULL, NULL, "
                "'https://meetings.iac.es/imbase2027/', "
                "'https://meetings.iac.es/imbase2027/', NULL, NULL, 'Tenerife', NULL, "
                "'DISCOVERED', NULL, '2026-10-14 00:00:00', '2026-10-14 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO deadlines VALUES "
                "(9, 7, 'REGISTRATION', '2027-05-01', "
                "'https://meetings.iac.es/imbase2027/pages/important-dates.php', 1, "
                "'2026-10-14 00:00:00', '2026-10-14 00:00:00', '2026-10-14 00:00:00')"
            )
        )
        connection.execute(
            text("INSERT INTO subscriptions VALUES (3, 1, 7, 'TRACKING', '2026-10-14 00:00:00')")
        )
    engine.dispose()

    run_migrations(_url(database))

    migrated = create_engine(f"sqlite:///{database}")
    inspector = inspect(migrated)
    deadline_columns = {item["name"]: item for item in inspector.get_columns("deadlines")}
    candidate_indexes = {item["name"] for item in inspector.get_indexes("event_candidates")}
    with migrated.connect() as connection:
        conference = connection.execute(
            text("SELECT name FROM conferences WHERE id = 7")
        ).scalar_one()
        subscription = connection.execute(
            text("SELECT status FROM subscriptions WHERE id = 3")
        ).scalar_one()
        deadline = connection.execute(
            text("SELECT deadline_date, verification_status FROM deadlines WHERE id = 9")
        ).one()
    migrated.dispose()

    assert VERIFIED_MONITORING_TABLES <= set(inspector.get_table_names())
    assert conference == "IMBASE 2027"
    assert subscription == "TRACKING"
    assert str(deadline.deadline_date) == "2027-05-01"
    assert deadline.verification_status == "OFFICIAL_CONFIRMED"
    assert deadline_columns["deadline_date"]["nullable"] is True
    assert {"approximate_year", "approximate_month", "tentative"} <= set(deadline_columns)
    assert {
        "ix_event_candidates_user_status",
        "ix_event_candidates_expires_at",
    } <= candidate_indexes


def test_catalog_candidate_migration_allows_distinct_titles_for_one_url(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog-candidates.db"
    config = _alembic_config(database)
    command.upgrade(config, "20261014_0004")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, telegram_id, created_at) "
                "VALUES (1, 123, '2026-10-15 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO event_candidates "
                "(id, user_id, title, normalized_title, source_url, query, relevance_score, "
                "summary, verification_status, status, expires_at, created_at, updated_at) "
                "VALUES (7, 1, 'First event', 'first event', 'https://example.org/events', "
                "'stars', 0.9, NULL, 'UNVERIFIABLE', 'ACTIVE', "
                "'2026-10-22 00:00:00', '2026-10-15 00:00:00', '2026-10-15 00:00:00')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    migrated = create_engine(f"sqlite:///{database}")
    with migrated.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO event_candidates "
                "(user_id, title, normalized_title, source_url, query, relevance_score, "
                "summary, verification_status, status, expires_at, created_at, updated_at) "
                "VALUES (1, 'Second event', 'second event', 'https://example.org/events', "
                "'stars', 0.8, NULL, 'UNVERIFIABLE', 'ACTIVE', "
                "'2026-10-22 00:00:00', '2026-10-15 00:00:00', '2026-10-15 00:00:00')"
            )
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM event_candidates")) == 2
        assert connection.scalar(
            text("SELECT id FROM event_candidates WHERE normalized_title = 'first event'")
        ) == 7
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO event_candidates "
                    "(user_id, title, normalized_title, source_url, query, relevance_score, "
                    "summary, verification_status, status, expires_at, created_at, updated_at) "
                    "VALUES (1, 'First event', 'first event', 'https://example.org/events', "
                    "'stars', 0.7, NULL, 'UNVERIFIABLE', 'ACTIVE', "
                    "'2026-10-22 00:00:00', '2026-10-15 00:00:00', '2026-10-15 00:00:00')"
                )
            )
    migrated.dispose()


def test_catalog_candidate_downgrade_refuses_duplicate_urls(tmp_path: Path) -> None:
    database = tmp_path / "catalog-candidates-downgrade.db"
    config = _alembic_config(database)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, telegram_id, created_at) "
                "VALUES (1, 123, '2026-10-15 00:00:00')"
            )
        )
        for candidate_id, title in ((1, "First event"), (2, "Second event")):
            connection.execute(
                text(
                    "INSERT INTO event_candidates "
                    "(id, user_id, title, normalized_title, source_url, query, relevance_score, "
                    "summary, verification_status, status, expires_at, created_at, updated_at) "
                    "VALUES (:id, 1, :title, :normalized_title, 'https://example.org/events', "
                    "'stars', 0.9, NULL, 'UNVERIFIABLE', 'ACTIVE', "
                    "'2026-10-22 00:00:00', '2026-10-15 00:00:00', "
                    "'2026-10-15 00:00:00')"
                ),
                {
                    "id": candidate_id,
                    "title": title,
                    "normalized_title": title.lower(),
                },
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match=r"cannot downgrade.*duplicate catalog URLs"):
        command.downgrade(config, "20261014_0004")


def test_candidate_snapshot_migration_preserves_existing_candidate(tmp_path: Path) -> None:
    database = tmp_path / "candidate-snapshot.db"
    config = _alembic_config(database)
    command.upgrade(config, "20261016_0009")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, telegram_id, created_at) "
                "VALUES (1, 123, '2026-10-19 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO event_candidates "
                "(id, user_id, title, normalized_title, source_url, query, relevance_score, "
                "summary, verification_status, status, expires_at, created_at, updated_at) "
                "VALUES (7, 1, 'IMBASE 2027', 'imbase 2027', "
                "'https://meetings.iac.es/imbase2027/', 'binary stars', 0.9, NULL, "
                "'UNVERIFIABLE', 'ACTIVE', '2026-10-22 00:00:00', "
                "'2026-10-19 00:00:00', '2026-10-19 00:00:00')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    migrated = create_engine(f"sqlite:///{database}")
    columns = {item["name"] for item in inspect(migrated).get_columns("event_candidates")}
    with migrated.connect() as connection:
        assert connection.scalar(
            text("SELECT title FROM event_candidates WHERE id = 7")
        ) == "IMBASE 2027"
    migrated.dispose()
    assert {
        "start_date",
        "end_date",
        "location",
        "official_url",
        "research_payload",
        "researched_at",
        "topic_relevance_score",
        "topic_relevance_reason",
    } <= columns


def test_candidate_decision_migration_marks_legacy_skipped_without_reactivating(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate-decisions.db"
    config = _alembic_config(database)
    command.upgrade(config, "20261019_0010")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, telegram_id, created_at) "
                "VALUES (1, 123, '2026-10-20 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO event_candidates "
                "(id, user_id, title, normalized_title, source_url, query, relevance_score, "
                "summary, verification_status, status, expires_at, created_at, updated_at) "
                "VALUES (7, 1, 'Legacy skipped', 'legacy skipped', "
                "'https://example.org/legacy', 'stars', 0.5, NULL, "
                "'UNVERIFIABLE', 'SKIPPED', '2026-10-25 00:00:00', "
                "'2026-10-19 00:00:00', '2026-10-20 01:02:03')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    migrated = create_engine(f"sqlite:///{database}")
    columns = {item["name"] for item in inspect(migrated).get_columns("event_candidates")}
    with migrated.begin() as connection:
        legacy = connection.execute(
            text(
                "SELECT status, decision_reason, decision_source, decision_at, next_review_at "
                "FROM event_candidates WHERE id = 7"
            )
        ).one()
        connection.execute(
            text(
                "UPDATE event_candidates SET status = 'REJECTED', "
                "decision_reason = 'USER_REJECTED', decision_source = 'USER' WHERE id = 7"
            )
        )
        assert connection.scalar(
            text("SELECT status FROM event_candidates WHERE id = 7")
        ) == "REJECTED"
    migrated.dispose()

    assert {
        "decision_reason",
        "decision_source",
        "decision_at",
        "next_review_at",
    } <= columns
    assert legacy.status == "SKIPPED"
    assert legacy.decision_reason == "LEGACY_SKIPPED"
    assert legacy.decision_source == "SYSTEM"
    assert str(legacy.decision_at).startswith("2026-10-20 01:02:03")
    assert legacy.next_review_at is None
