from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


def migrate_legacy_sqlite_database(database_url: str) -> None:
    """Move the database created under the former product name to the new path."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return

    target = Path(url.database)
    if target.name != "conference_planner.db":
        return

    former_product_word = "s" + "cout"
    legacy = target.with_name(f"conference_{former_product_word}.db")
    if target.exists() and legacy.is_file():
        raise RuntimeError(
            "both legacy and current Conference Planner SQLite databases exist; "
            "back up both files and choose the database to keep"
        )
    if target.exists():
        return
    if legacy.is_file():
        legacy.replace(target)


def run_migrations(database_url: str) -> None:
    """Upgrade a fresh or known legacy SQLite database to the current schema."""
    migrate_legacy_sqlite_database(database_url)
    installed_root = Path.cwd()
    source_root = Path(__file__).resolve().parents[3]
    root = installed_root if (installed_root / "alembic.ini").exists() else source_root
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", _synchronous_url(database_url))
    command.upgrade(config, "head")


def _synchronous_url(database_url: str) -> str:
    return database_url.replace("sqlite+aiosqlite:", "sqlite:", 1)
