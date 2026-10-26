from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.base import utc_now
from conference_planner.db.models import WebDocumentCache
from conference_planner.web.fetcher import FetchedPage


class WebDocumentCacheRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, url: str) -> WebDocumentCache | None:
        return await self.session.scalar(
            select(WebDocumentCache).where(WebDocumentCache.canonical_url == url)
        )

    async def store(
        self,
        page: FetchedPage,
        extracted_text: str,
        *,
        observed_at: datetime | None = None,
    ) -> WebDocumentCache:
        cached = await self.get(page.url)
        if cached is None:
            cached = WebDocumentCache(
                canonical_url=page.url,
                content_hash=page.content_hash,
                extracted_text=extracted_text,
            )
            self.session.add(cached)
        cached.etag = page.etag
        cached.last_modified = page.last_modified
        cached.content_hash = page.content_hash
        cached.extracted_text = extracted_text
        cached.status_code = page.status_code
        cached.observed_at = observed_at or utc_now()
        await self.session.flush()
        return cached

    async def touch(
        self,
        cached: WebDocumentCache,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        status_code: int = 304,
        observed_at: datetime | None = None,
    ) -> None:
        if etag is not None:
            cached.etag = etag
        if last_modified is not None:
            cached.last_modified = last_modified
        cached.status_code = status_code
        cached.observed_at = observed_at or utc_now()
        await self.session.flush()
