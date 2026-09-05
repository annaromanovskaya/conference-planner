import pytest

from conference_scout.healthcheck import check_database


@pytest.mark.asyncio
async def test_healthcheck_accepts_writable_sqlite(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'health.db'}"
    assert await check_database(url) is True


@pytest.mark.asyncio
async def test_healthcheck_rejects_unusable_database() -> None:
    assert await check_database("sqlite+aiosqlite:////missing/path/health.db") is False
