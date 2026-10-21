import asyncio

from sqlalchemy import text

from conference_scout.config import Settings
from conference_scout.db.session import create_engine_and_sessionmaker


async def check_database(database_url: str) -> bool:
    engine, session_factory = create_engine_and_sessionmaker(database_url)
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def run() -> None:
    settings = Settings()
    raise SystemExit(0 if asyncio.run(check_database(settings.database_url)) else 1)


if __name__ == "__main__":
    run()
