from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from conference_planner.db.base import Base


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    if dbapi_connection.__class__.__module__.startswith("aiosqlite") or dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine_and_sessionmaker(
    url: str,
) -> tuple[AsyncEngine, async_sessionmaker]:
    options = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, **options)
    if url.startswith("sqlite"):
        # WAL is persistent for a file-backed database. Set it outside any
        # transaction at connection creation, including existing deployments.
        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                if ":memory:" not in url:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
