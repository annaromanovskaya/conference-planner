from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Conference, Deadline, DeadlineHistory, Subscription
from conference_scout.schemas.common import ConferenceStatus


class ConferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_status(self, status: ConferenceStatus) -> list[Conference]:
        return list(
            await self.session.scalars(
                select(Conference)
                .where(Conference.status == status)
                .order_by(Conference.start_date)
            )
        )

    async def track(self, user_id: int, conference_id: int) -> Subscription:
        conference = await self.session.get(Conference, conference_id)
        if conference is None:
            raise ValueError("conference not found")
        subscription = await self.session.scalar(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.conference_id == conference_id,
            )
        )
        if subscription is None:
            subscription = Subscription(user_id=user_id, conference_id=conference_id)
            self.session.add(subscription)
        subscription.status = ConferenceStatus.TRACKING
        conference.status = ConferenceStatus.TRACKING
        await self.session.flush()
        return subscription

    async def archive(self, conference_id: int) -> None:
        conference = await self.session.get(Conference, conference_id)
        if conference is not None:
            conference.status = ConferenceStatus.ARCHIVED

    async def update_deadline(
        self,
        deadline: Deadline,
        new_date: date,
        source_url: str,
        *,
        verified: bool,
    ) -> DeadlineHistory | None:
        checked_at = datetime.now(UTC)
        if deadline.deadline_date == new_date:
            deadline.source_url = source_url
            deadline.verified = verified
            deadline.last_checked_at = checked_at
            return None
        history = DeadlineHistory(
            conference_id=deadline.conference_id,
            deadline_type=deadline.deadline_type,
            old_date=deadline.deadline_date,
            new_date=new_date,
            detected_at=checked_at,
            source_url=source_url,
        )
        deadline.deadline_date = new_date
        deadline.source_url = source_url
        deadline.verified = verified
        deadline.last_checked_at = checked_at
        self.session.add(history)
        await self.session.flush()
        return history
