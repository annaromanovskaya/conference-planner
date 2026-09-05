from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from conference_scout.conferences.verification import is_official_source
from conference_scout.db.models import Conference, Subscription
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.schemas.common import ConferenceStatus, DeadlineType
from conference_scout.schemas.conferences import ConferenceExtraction
from conference_scout.web.fetcher import FetchedPage
from conference_scout.web.parser import extract_page_text


class MonitoringFetcherProtocol(Protocol):
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage: ...


class MonitoringAnalyzer(Protocol):
    async def extract_conference(self, source_url: str, text: str) -> ConferenceExtraction: ...


@dataclass(frozen=True, slots=True)
class DeadlineChange:
    conference_id: int
    conference_name: str
    deadline_type: DeadlineType
    old_date: date
    new_date: date
    source_url: str


@dataclass(slots=True)
class MonitoringReport:
    changes: list[DeadlineChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MonitoringService:
    def __init__(
        self,
        session: AsyncSession,
        fetcher: MonitoringFetcherProtocol,
        ai: MonitoringAnalyzer,
        max_page_bytes: int,
        max_page_chars: int,
    ) -> None:
        self.session = session
        self.fetcher = fetcher
        self.ai = ai
        self.max_page_bytes = max_page_bytes
        self.max_page_chars = max_page_chars

    async def run(self, user_id: int) -> MonitoringReport:
        report = MonitoringReport()
        conferences = list(
            await self.session.scalars(
                select(Conference)
                .join(Subscription, Subscription.conference_id == Conference.id)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.status == ConferenceStatus.TRACKING,
                )
                .options(selectinload(Conference.deadlines))
            )
        )
        for conference in conferences:
            urls = {conference.website} | {deadline.source_url for deadline in conference.deadlines}
            for url in filter(None, urls):
                try:
                    page = await self.fetcher.fetch(url, max_bytes=self.max_page_bytes)
                    extraction = await self.ai.extract_conference(
                        page.url, extract_page_text(page.html, max_chars=self.max_page_chars)
                    )
                    await self._apply(conference, extraction, report)
                except Exception as exc:
                    report.errors.append(f"{conference.id}:{url}:{type(exc).__name__}")
        await self.session.commit()
        return report

    async def _apply(
        self, conference: Conference, extraction: ConferenceExtraction, report: MonitoringReport
    ) -> None:
        deadlines = {item.deadline_type: item for item in conference.deadlines}
        repository = ConferenceRepository(self.session)
        for extracted in extraction.deadlines:
            current = deadlines.get(extracted.deadline_type)
            if current is None or extracted.deadline_date is None or extracted.source_url is None:
                continue
            old_date = current.deadline_date
            source_url = str(extracted.source_url)
            history = await repository.update_deadline(
                current,
                extracted.deadline_date,
                source_url,
                verified=is_official_source(source_url, conference.website),
            )
            if history is not None:
                report.changes.append(
                    DeadlineChange(
                        conference.id,
                        conference.name,
                        current.deadline_type,
                        old_date,
                        extracted.deadline_date,
                        source_url,
                    )
                )
