from collections import deque
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.ai.active_provider import ActiveLLMProvider
from conference_scout.ai.models import AvailableModel, ChatMessage, ChatResponse, ToolDefinition
from conference_scout.ai.registry import ProviderProfile
from conference_scout.ai.service import AIService
from conference_scout.app import _model_registry, _run_scheduled_conference_cycle
from conference_scout.conferences.cycle import ConferenceCycleReport, CycleMode
from conference_scout.conferences.discovery import DiscoveryService
from conference_scout.db.models import (
    Conference,
    Deadline,
    Topic,
    TrustedSource,
    User,
)
from conference_scout.db.repositories.ollama_usage import OllamaUsageRepository
from conference_scout.schemas.common import AgentRunStatus, DeadlineType, SourceType
from conference_scout.schemas.conferences import ConferenceExtraction, ExtractedDeadline
from conference_scout.schemas.integrations import SearchResult
from conference_scout.schemas.topics import RelevanceAssessment
from conference_scout.web.fetcher import FetchedPage, FetchError


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
    async def extract_conference(
        self, source_url: str, _text: str, *, expected_name: str | None = None
    ) -> ConferenceExtraction:
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


class ScheduledCycle:
    async def run(self, user_id: int, request, progress=None) -> ConferenceCycleReport:
        assert user_id > 0
        assert request.mode == CycleMode.SCHEDULED
        assert request.query is None
        assert progress is None
        return ConferenceCycleReport(
            run_id=8,
            status=AgentRunStatus.COMPLETED,
            new_candidate_ids=(41, 42),
        )


@pytest.mark.asyncio
async def test_scheduled_cycle_uses_the_same_service_as_manual_search(
) -> None:
    report = await _run_scheduled_conference_cycle(
        124, ScheduledCycle()
    )

    assert report.new_candidate_ids == (41, 42)


class StructuredGateway:
    def __init__(self, responses: list[str]) -> None:
        self.responses = deque(responses)

    async def list_models(self):
        return (AvailableModel(name="gemma4:31b"),)

    async def chat(
        self,
        _model: str,
        _messages: list[ChatMessage],
        _tools: list[ToolDefinition] = (),
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> ChatResponse:
        return ChatResponse(content=self.responses.popleft(), prompt_tokens=9, output_tokens=3)


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


@pytest.mark.asyncio
async def test_import_candidate_persists_exact_url_for_all_relevant_topics(
    session: AsyncSession,
) -> None:
    owner = User(telegram_id=456)
    session.add(owner)
    await session.flush()
    session.add_all(
        [
            Topic(user_id=owner.id, name="Stars", description="Stars", keywords=["stars"]),
            Topic(user_id=owner.id, name="ML", description="ML", keywords=["machine learning"]),
        ]
    )
    await session.commit()
    service = DiscoveryService(session, FakeSearch(), FakeFetcher(), FakeAI(), 0.5, 10_000, 10_000)

    conference_id = await service.import_candidate(owner.id, "https://event.example/official")

    conference = await session.get(Conference, conference_id)
    assert conference is not None
    assert conference.source_url == "https://event.example/official"
    from conference_scout.db.models import ConferenceTopic

    links = list(
        await session.scalars(
            select(ConferenceTopic).where(ConferenceTopic.conference_id == conference_id)
        )
    )
    assert len(links) == 2


@pytest.mark.asyncio
async def test_import_candidate_saves_unverified_placeholder_when_tls_fetch_fails(
    session: AsyncSession,
) -> None:
    class TLSFailureFetcher:
        async def fetch(self, _url: str, *, max_bytes: int) -> FetchedPage:
            raise FetchError("page request failed")

    owner = User(telegram_id=457)
    session.add(owner)
    await session.flush()
    session.add(Topic(user_id=owner.id, name="Stars", description="Stars", keywords=["stars"]))
    await session.commit()
    service = DiscoveryService(
        session, FakeSearch(), TLSFailureFetcher(), FakeAI(), 0.5, 10_000, 10_000
    )

    conference_id = await service.import_candidate(
        owner.id,
        "https://meetings.example/imbase2027/",
        expected_name="IMBASE 2027",
    )

    conference = await session.get(Conference, conference_id)
    assert conference is not None
    assert conference.name == "IMBASE 2027"
    assert conference.website == "https://meetings.example/imbase2027/"
    assert conference.source_url == "https://meetings.example/imbase2027/"
    assert conference.last_checked_at is None


@pytest.mark.asyncio
async def test_ollama_usage_is_wired_for_discovery_structured_calls(
    session: AsyncSession,
) -> None:
    owner = User(telegram_id=889)
    session.add(owner)
    await session.flush()
    session.add(
        Topic(
            user_id=owner.id,
            name="Astronomy",
            description="Stars",
            keywords=["astronomy"],
            enabled=True,
        )
    )
    gateway = StructuredGateway(
        [
            '{"name":"Astro 2027","source_url":"https://event.example",'
            '"official_url":"https://event.example","deadlines":[]}',
            '{"score":0.9,"explanation":"Matches astronomy"}',
        ]
    )
    registry = _model_registry(
        session,
        (ProviderProfile("ollama-cloud", "Ollama Cloud", gateway, ("gemma4:31b",)),),
    )
    await registry.select(owner.id, "ollama-cloud", "gemma4:31b")
    service = DiscoveryService(
        session,
        FakeSearch(),
        FakeFetcher(),
        AIService(ActiveLLMProvider(registry, owner.id)),
        0.5,
        10_000,
        10_000,
    )

    await service.run(owner.id, years=[2027])
    rows = await OllamaUsageRepository(session).get_daily_totals(
        owner.id, "ollama-cloud", datetime.now(UTC).date()
    )

    assert rows[0].request_count == 2
    assert rows[0].prompt_tokens == 18
