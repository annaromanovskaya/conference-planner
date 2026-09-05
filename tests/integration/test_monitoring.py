from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.monitoring import MonitoringService
from conference_scout.db.models import Conference, Deadline, DeadlineHistory, Subscription, User
from conference_scout.schemas.common import ConferenceStatus, DeadlineType
from conference_scout.schemas.conferences import ConferenceExtraction, ExtractedDeadline
from conference_scout.web.fetcher import FetchedPage


class MonitoringFetcher:
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage:
        return FetchedPage(url, "<p>Abstract deadline: 2027-02-19</p>")


class MonitoringAI:
    async def extract_conference(self, source_url: str, _text: str) -> ConferenceExtraction:
        return ConferenceExtraction(
            name="Astro 2027",
            source_url=source_url,
            official_url="https://event.example/",
            deadlines=[
                ExtractedDeadline(
                    deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
                    deadline_date=date(2027, 2, 19),
                    source_url=source_url,
                )
            ],
        )


@pytest.mark.asyncio
async def test_monitoring_updates_deadline_history_and_requests_notification(
    session: AsyncSession,
) -> None:
    owner = User(telegram_id=123)
    conference = Conference(
        name="Astro 2027",
        normalized_name="astro 2027",
        website="https://event.example/",
        status=ConferenceStatus.TRACKING,
    )
    session.add_all([owner, conference])
    await session.flush()
    session.add(Subscription(user_id=owner.id, conference_id=conference.id))
    session.add(
        Deadline(
            conference_id=conference.id,
            deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
            deadline_date=date(2027, 2, 12),
            source_url="https://event.example/dates",
            verified=True,
        )
    )
    await session.commit()

    report = await MonitoringService(
        session, MonitoringFetcher(), MonitoringAI(), 10_000, 10_000
    ).run(owner.id)

    assert len(report.changes) == 1
    assert report.changes[0].old_date == date(2027, 2, 12)
    assert report.changes[0].new_date == date(2027, 2, 19)
    assert await session.scalar(select(func.count()).select_from(DeadlineHistory)) == 1
