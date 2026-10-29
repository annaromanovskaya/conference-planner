import logging
from dataclasses import dataclass, field
from typing import Protocol
from collections.abc import Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.ai.service import AIResponseError
from conference_planner.conferences.deduplication import normalize_name
from conference_planner.conferences.verification import is_official_source
from conference_planner.db.base import utc_now
from conference_planner.db.models import Conference, ConferenceTopic, Deadline, Topic, TrustedSource
from conference_planner.schemas.common import ConferenceStatus, SourceType
from conference_planner.schemas.conferences import ConferenceExtraction
from conference_planner.schemas.topics import RelevanceAssessment
from conference_planner.search.provider import SearchProvider
from conference_planner.search.query import generate_queries
from conference_planner.web.fetcher import FetchedPage, FetchError
from conference_planner.web.parser import extract_page_text

logger = logging.getLogger(__name__)


class Fetcher(Protocol):
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage: ...


class Analyzer(Protocol):
    async def extract_conference(
        self, source_url: str, text: str, *, expected_name: str | None = None
    ) -> ConferenceExtraction: ...

    async def rank_relevance(
        self, extraction: ConferenceExtraction, topic: Topic
    ) -> RelevanceAssessment: ...


@dataclass(frozen=True, slots=True)
class DiscoveryError:
    stage: str
    target: str
    message: str


@dataclass(slots=True)
class DiscoveryReport:
    created_ids: list[int] = field(default_factory=list)
    merged_ids: list[int] = field(default_factory=list)
    rejected_urls: list[str] = field(default_factory=list)
    errors: list[DiscoveryError] = field(default_factory=list)


class DiscoveryService:
    def __init__(
        self,
        session: AsyncSession,
        search: SearchProvider,
        fetcher: Fetcher,
        ai: Analyzer,
        min_relevance: float,
        max_page_bytes: int,
        max_page_chars: int,
    ) -> None:
        self.session = session
        self.search = search
        self.fetcher = fetcher
        self.ai = ai
        self.min_relevance = min_relevance
        self.max_page_bytes = max_page_bytes
        self.max_page_chars = max_page_chars

    async def run(
        self, user_id: int, *, years: list[int],
        on_progress: Callable[[DiscoveryReport, str], Awaitable[None]] | None = None,
    ) -> DiscoveryReport:
        report = DiscoveryReport()
        topics = list(
            await self.session.scalars(
                select(Topic).where(Topic.user_id == user_id, Topic.enabled.is_(True))
            )
        )
        sources = list(
            await self.session.scalars(select(TrustedSource).where(TrustedSource.enabled.is_(True)))
        )
        domains = [source.url for source in sources if source.source_type == SourceType.DOMAIN]
        pages = [source.url for source in sources if source.source_type == SourceType.PAGE]
        seen_urls: set[str] = set()
        for topic in topics:
            for page_url in pages:
                if page_url not in seen_urls:
                    seen_urls.add(page_url)
                    await self._process_candidate(page_url, topic, report)
                    await self.session.commit()
                    if on_progress:
                        await on_progress(report, page_url)
            for query in generate_queries(topic.keywords, years, domains):
                try:
                    results = await self.search.search(query)
                except Exception as exc:  # provider boundary isolation
                    report.errors.append(DiscoveryError("search", query, str(exc)))
                    continue
                for result in results:
                    url = str(result.url)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    await self._process_candidate(url, topic, report)
                    await self.session.commit()
                    if on_progress:
                        await on_progress(report, url)
        await self.session.commit()
        return report

    async def import_candidate(
        self, user_id: int, url: str, *, expected_name: str | None = None
    ) -> int:
        topics = list(
            await self.session.scalars(
                select(Topic).where(Topic.user_id == user_id, Topic.enabled.is_(True))
            )
        )
        if not topics:
            raise ValueError("add at least one topic before importing conferences")

        try:
            page = await self.fetcher.fetch(url, max_bytes=self.max_page_bytes)
            text = extract_page_text(page.html, max_chars=self.max_page_chars)
            extraction = await self.ai.extract_conference(
                page.url, text, expected_name=expected_name
            )
        except (FetchError, AIResponseError):
            if expected_name is None:
                raise
            logger.warning("conference saved without verified page details", exc_info=True)
            return await self._save_unverified_candidate(expected_name, url)
        relevant: list[tuple[Topic, float]] = []
        for topic in topics:
            assessment = await self.ai.rank_relevance(extraction, topic)
            if assessment.score >= self.min_relevance:
                relevant.append((topic, assessment.score))
        if not relevant:
            raise ValueError("conference is not relevant to configured topics")

        website = str(extraction.official_url or extraction.source_url)
        conference = await self.session.scalar(
            select(Conference).where(
                Conference.website == website,
                Conference.is_test.is_(False),
            )
        )
        if conference is None:
            conference = Conference(
                name=extraction.name,
                normalized_name=normalize_name(extraction.name),
                acronym=extraction.acronym,
                event_type=extraction.event_type,
                website=website,
                source_url=str(extraction.source_url),
                start_date=extraction.start_date,
                end_date=extraction.end_date,
                location=extraction.location,
                description=extraction.description,
                status=ConferenceStatus.DISCOVERED,
                last_checked_at=utc_now(),
            )
            self.session.add(conference)
            await self.session.flush()
            for extracted in extraction.deadlines:
                if extracted.deadline_date is None or extracted.source_url is None:
                    continue
                source_url = str(extracted.source_url)
                self.session.add(
                    Deadline(
                        conference_id=conference.id,
                        deadline_type=extracted.deadline_type,
                        deadline_date=extracted.deadline_date,
                        source_url=source_url,
                        verified=is_official_source(source_url, website),
                        last_checked_at=utc_now(),
                    )
                )

        existing_topic_ids = set(
            await self.session.scalars(
                select(ConferenceTopic.topic_id).where(
                    ConferenceTopic.conference_id == conference.id
                )
            )
        )
        for topic, score in relevant:
            if topic.id not in existing_topic_ids:
                self.session.add(
                    ConferenceTopic(
                        conference_id=conference.id,
                        topic_id=topic.id,
                        relevance_score=score,
                    )
                )
        await self.session.flush()
        return conference.id

    async def _save_unverified_candidate(self, name: str, url: str) -> int:
        conference = await self.session.scalar(
            select(Conference).where(
                Conference.is_test.is_(False),
                or_(Conference.website == url, Conference.source_url == url),
            )
        )
        if conference is None:
            conference = Conference(
                name=name,
                normalized_name=normalize_name(name),
                website=url,
                source_url=url,
                status=ConferenceStatus.DISCOVERED,
                last_checked_at=None,
            )
            self.session.add(conference)
            await self.session.flush()
        return conference.id

    async def _process_candidate(self, url: str, topic: Topic, report: DiscoveryReport) -> None:
        try:
            page = await self.fetcher.fetch(url, max_bytes=self.max_page_bytes)
            text = extract_page_text(page.html, max_chars=self.max_page_chars)
            extraction = await self.ai.extract_conference(page.url, text)
            relevance = await self.ai.rank_relevance(extraction, topic)
            if relevance.score < self.min_relevance:
                report.rejected_urls.append(url)
                return
            website = str(extraction.official_url or extraction.source_url)
            existing = await self.session.scalar(
                select(Conference).where(
                    Conference.website == website,
                    Conference.is_test.is_(False),
                )
            )
            if existing is not None:
                report.merged_ids.append(existing.id)
                return
            conference = Conference(
                name=extraction.name,
                normalized_name=normalize_name(extraction.name),
                acronym=extraction.acronym,
                event_type=extraction.event_type,
                website=website,
                source_url=str(extraction.source_url),
                start_date=extraction.start_date,
                end_date=extraction.end_date,
                location=extraction.location,
                description=extraction.description,
                status=ConferenceStatus.DISCOVERED,
                last_checked_at=utc_now(),
            )
            self.session.add(conference)
            await self.session.flush()
            self.session.add(
                ConferenceTopic(
                    conference_id=conference.id,
                    topic_id=topic.id,
                    relevance_score=relevance.score,
                )
            )
            for extracted in extraction.deadlines:
                if extracted.deadline_date is None or extracted.source_url is None:
                    continue
                source_url = str(extracted.source_url)
                self.session.add(
                    Deadline(
                        conference_id=conference.id,
                        deadline_type=extracted.deadline_type,
                        deadline_date=extracted.deadline_date,
                        source_url=source_url,
                        verified=is_official_source(source_url, website),
                        last_checked_at=utc_now(),
                    )
                )
            await self.session.flush()
            report.created_ids.append(conference.id)
        except Exception as exc:  # candidate boundary isolation
            logger.warning("candidate rejected", extra={"url": url, "error": type(exc).__name__})
            report.errors.append(DiscoveryError("candidate", url, str(exc)))
