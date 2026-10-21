from pathlib import Path

from alembic import command
from alembic.config import Config


def run_migrations(database_url: str) -> None:
    """Upgrade a fresh or known legacy SQLite database to the current schema."""
    installed_root = Path.cwd()
    source_root = Path(__file__).resolve().parents[3]
    root = installed_root if (installed_root / "alembic.ini").exists() else source_root
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", _synchronous_url(database_url))
    command.upgrade(config, "head")


def _synchronous_url(database_url: str) -> str:
    return database_url.replace("sqlite+aiosqlite:", "sqlite:", 1)
