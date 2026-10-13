from datetime import UTC, date, datetime

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import (
    Conference,
    ConferenceTopic,
    Deadline,
    DeadlineHistory,
    Subscription,
    Topic,
)
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

    async def list_accessible_by_status(
        self, user_id: int, status: ConferenceStatus
    ) -> list[Conference]:
        if status in {ConferenceStatus.TRACKING, ConferenceStatus.ARCHIVED}:
            return list(
                await self.session.scalars(
                    select(Conference)
                    .join(Subscription, Subscription.conference_id == Conference.id)
                    .where(
                        Subscription.user_id == user_id,
                        Subscription.status == status,
                    )
                    .order_by(Conference.start_date)
                )
            )
        ids = await self.accessible_ids(user_id)
        if not ids:
            return []
        subscribed = select(Subscription.conference_id).where(Subscription.user_id == user_id)
        return list(
            await self.session.scalars(
                select(Conference)
                .where(
                    Conference.id.in_(ids),
                    Conference.status == status,
                    Conference.id.not_in(subscribed),
                )
                .order_by(Conference.start_date)
            )
        )

    async def accessible_ids(self, user_id: int) -> set[int]:
        by_topic = (
            select(ConferenceTopic.conference_id)
            .join(Topic, Topic.id == ConferenceTopic.topic_id)
            .where(Topic.user_id == user_id)
        )
        by_subscription = select(Subscription.conference_id).where(Subscription.user_id == user_id)
        return set(await self.session.scalars(union(by_topic, by_subscription)))

    async def require_accessible(self, user_id: int, conference_id: int) -> Conference:
        if conference_id not in await self.accessible_ids(user_id):
            raise ValueError("conference not found")
        conference = await self.session.get(Conference, conference_id)
        if conference is None:
            raise ValueError("conference not found")
        return conference

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
        await self.session.flush()
        return subscription

    async def archive(self, user_id: int, conference_id: int) -> Subscription:
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
        subscription.status = ConferenceStatus.ARCHIVED
        await self.session.flush()
        return subscription

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
