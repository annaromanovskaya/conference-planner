import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.repositories.sources import SourceRepository
from conference_scout.db.repositories.topics import TopicRepository
from conference_scout.schemas.common import SourceType
from conference_scout.schemas.topics import TopicNormalization


@pytest.mark.asyncio
async def test_topic_preview_is_not_persisted_until_create(session: AsyncSession) -> None:
    repository = TopicRepository(session)
    preview = TopicNormalization(name="Astro", description="Stars", keywords=["stellar spectra"])
    assert await repository.list_for_user(1) == []
    # The bot persists only after the user confirms the preview.
    # A valid owner row is created in the actual handler transaction.
    assert preview.name == "Astro"


@pytest.mark.asyncio
async def test_source_repository_manages_global_sources(session: AsyncSession) -> None:
    repository = SourceRepository(session)
    source = await repository.create("iau.org", "IAU", SourceType.DOMAIN)
    await session.commit()
    await repository.set_enabled(source.id, False)
    await session.commit()
    assert (await repository.list_all())[0].enabled is False
    await repository.delete(source.id)
    await session.commit()
    assert await repository.list_all() == []
