from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.session import create_engine_and_sessionmaker, initialize_database


@pytest_asyncio.fixture
async def session(tmp_path) -> AsyncIterator[AsyncSession]:
    engine, session_factory = create_engine_and_sessionmaker(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    )
    await initialize_database(engine)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()
