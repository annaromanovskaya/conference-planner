from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.discovery import DiscoveryService
from conference_scout.db.models import Conference, Deadline, Topic, TrustedSource, User
from conference_scout.schemas.common import DeadlineType, SourceType
from conference_scout.schemas.conferences import ConferenceExtraction, ExtractedDeadline
from conference_scout.schemas.integrations import SearchResult
from conference_scout.schemas.topics import RelevanceAssessment
from conference_scout.web.fetcher import FetchedPage


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, _query: str) -> list[SearchResult]:
        self.queries.append(_query)
        return [
            SearchResult(title="Astro 2027", url="https://event.example"),
            SearchResult(title="Broken", url="https://broken.example"),
        ]


class FakeFetcher:
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage:
        if "broken" in url:
            raise RuntimeError("unavailable")
        return FetchedPage(url, "<h1>Astro 2027</h1><p>Deadline 2027-02-12</p>")


class FakeAI:
    async def extract_conference(self, source_url: str, _text: str) -> ConferenceExtraction:
        return ConferenceExtraction(
            name="Astro 2027",
            source_url=source_url,
            official_url=source_url,
            start_date=date(2027, 6, 1),
            deadlines=[
                ExtractedDeadline(
                    deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
                    deadline_date=date(2027, 2, 12),
                    source_url=source_url,
                )
            ],
        )

    async def rank_relevance(self, *_args) -> RelevanceAssessment:
        return RelevanceAssessment(score=0.9, explanation="Matches astronomy")


@pytest.mark.asyncio
async def test_discovery_persists_source_backed_candidate_and_isolates_errors(
    session: AsyncSession,
) -> None:
    owner = User(telegram_id=123)
    session.add(owner)
    await session.flush()
    session.add(
        Topic(
            user_id=owner.id,
            name="Astronomy",
            description="Astronomical science",
            keywords=["astroinformatics"],
            enabled=True,
        )
    )
    await session.commit()
    service = DiscoveryService(
        session=session,
        search=FakeSearch(),
        fetcher=FakeFetcher(),
        ai=FakeAI(),
        min_relevance=0.5,
        max_page_bytes=10_000,
        max_page_chars=10_000,
    )

    report = await service.run(owner.id, years=[2027])

    assert len(report.created_ids) == 1
    assert len(report.errors) == 1
    assert await session.scalar(select(func.count()).select_from(Conference)) == 1
    deadline = await session.scalar(select(Deadline))
    assert deadline is not None
    assert deadline.verified is True


@pytest.mark.asyncio
async def test_discovery_deduplicates_repeated_search_url(session: AsyncSession) -> None:
    owner = User(telegram_id=123)
    session.add(owner)
    await session.flush()
    session.add(
        Topic(
            user_id=owner.id,
            name="Astronomy",
            description="Science",
            keywords=["same", "same"],
            enabled=True,
        )
    )
    await session.commit()
    service = DiscoveryService(session, FakeSearch(), FakeFetcher(), FakeAI(), 0.5, 10_000, 10_000)

    await service.run(owner.id, years=[2027])

    assert await session.scalar(select(func.count()).select_from(Conference)) == 1


@pytest.mark.asyncio
async def test_discovery_uses_trusted_domains_and_pages(session: AsyncSession) -> None:
    owner = User(telegram_id=123)
    session.add(owner)
    await session.flush()
    session.add(Topic(user_id=owner.id, name="Astro", description="Science", keywords=["stars"]))
    session.add_all(
        [
            TrustedSource(url="iau.org", name="IAU", source_type=SourceType.DOMAIN),
            TrustedSource(
                url="https://catalog.example/events",
                name="Catalog",
                source_type=SourceType.PAGE,
            ),
        ]
    )
    await session.commit()
    search = FakeSearch()
    fetcher = FakeFetcher()
    service = DiscoveryService(session, search, fetcher, FakeAI(), 0.5, 10_000, 10_000)

    await service.run(owner.id, years=[2027])

    assert "site:iau.org stars 2027" in search.queries
