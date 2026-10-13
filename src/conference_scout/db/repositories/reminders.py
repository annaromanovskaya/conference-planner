from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Conference, Deadline, Reminder, Subscription
from conference_scout.reminders.service import ReminderCandidate
from conference_scout.schemas.common import ConferenceStatus


class ReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_sent(
        self, user_id: int, deadline_id: int, days_before: int, sent_at: datetime
    ) -> Reminder:
        reminder = Reminder(
            user_id=user_id,
            deadline_id=deadline_id,
            days_before=days_before,
            sent_at=sent_at,
        )
        self.session.add(reminder)
        await self.session.flush()
        return reminder

    async def candidates(self, user_id: int) -> list[ReminderCandidate]:
        rows = await self.session.execute(
            select(Deadline, Conference.name)
            .join(Conference, Conference.id == Deadline.conference_id)
            .join(Subscription, Subscription.conference_id == Conference.id)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == ConferenceStatus.TRACKING,
            )
        )
        return [
            ReminderCandidate(
                deadline_id=deadline.id,
                conference_name=name,
                deadline_type=deadline.deadline_type.value,
                deadline_date=deadline.deadline_date,
            )
            for deadline, name in rows
        ]

    async def was_sent(self, user_id: int, deadline_id: int, days: int) -> bool:
        return (
            await self.session.scalar(
                select(Reminder.id).where(
                    Reminder.user_id == user_id,
                    Reminder.deadline_id == deadline_id,
                    Reminder.days_before == days,
                )
            )
            is not None
        )

    async def record(self, user_id: int, deadline_id: int, days: int) -> None:
        from conference_scout.db.base import utc_now

        await self.record_sent(user_id, deadline_id, days, utc_now())
        await self.session.commit()
