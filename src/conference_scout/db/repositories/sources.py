from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import TrustedSource
from conference_scout.schemas.common import SourceType


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[TrustedSource]:
        return list(await self.session.scalars(select(TrustedSource).order_by(TrustedSource.name)))

    async def create(self, url: str, name: str, source_type: SourceType) -> TrustedSource:
        source = TrustedSource(url=url, name=name, source_type=source_type, enabled=True)
        self.session.add(source)
        await self.session.flush()
        return source

    async def set_enabled(self, source_id: int, enabled: bool) -> None:
        source = await self.session.get(TrustedSource, source_id)
        if source is not None:
            source.enabled = enabled

    async def delete(self, source_id: int) -> None:
        source = await self.session.get(TrustedSource, source_id)
        if source is not None:
            await self.session.delete(source)
