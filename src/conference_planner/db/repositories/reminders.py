from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import Conference, Deadline, Reminder, Subscription
from conference_planner.reminders.service import ReminderCandidate
from conference_planner.schemas.common import ConferenceStatus


@dataclass(frozen=True, slots=True)
class DeadlineProjection:
    deadline_id: int
    conference_name: str
    deadline_type: str
    deadline_date: date | None
    approximate_year: int | None
    approximate_month: int | None
    tentative: bool
    source_url: str
    is_test: bool


class ReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_sent(
        self, user_id: int, deadline_id: int, days_before: int, sent_at: datetime
    ) -> Reminder:
        return await self.record_notice(
            user_id, deadline_id, "EXACT", days_before, sent_at
        )

    async def record_notice(
        self,
        user_id: int,
        deadline_id: int,
        notification_kind: str,
        threshold: int,
        sent_at: datetime,
    ) -> Reminder:
        reminder = Reminder(
            user_id=user_id,
            deadline_id=deadline_id,
            days_before=threshold,
            notification_kind=notification_kind,
            threshold=threshold,
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
                Conference.is_test.is_(False),
            )
        )
        return [
            ReminderCandidate(
                deadline_id=deadline.id,
                conference_name=name,
                deadline_type=deadline.deadline_type.value,
                deadline_date=deadline.deadline_date,
                approximate_year=deadline.approximate_year,
                approximate_month=deadline.approximate_month,
            )
            for deadline, name in rows
        ]

    async def upcoming(
        self,
        user_id: int,
        today: date,
        *,
        until: date | None = None,
    ) -> list[DeadlineProjection]:
        rows = await self.session.execute(
            select(Deadline, Conference.name, Conference.is_test)
            .join(Conference, Conference.id == Deadline.conference_id)
            .join(Subscription, Subscription.conference_id == Conference.id)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == ConferenceStatus.TRACKING,
                (
                    (Deadline.deadline_date >= today)
                    | (
                        Deadline.deadline_date.is_(None)
                        & (Deadline.approximate_year.is_not(None))
                        & (
                            (Deadline.approximate_year > today.year)
                            | (
                                (Deadline.approximate_year == today.year)
                                & (
                                    Deadline.approximate_month.is_(None)
                                    | (Deadline.approximate_month >= today.month)
                                )
                            )
                        )
                    )
                ),
            )
        )
        values = [
            DeadlineProjection(
                deadline_id=deadline.id,
                conference_name=name,
                deadline_type=deadline.deadline_type.value,
                deadline_date=deadline.deadline_date,
                approximate_year=deadline.approximate_year,
                approximate_month=deadline.approximate_month,
                tentative=deadline.tentative,
                source_url=deadline.source_url,
                is_test=is_test,
            )
            for deadline, name, is_test in rows
        ]
        if until is not None:
            values = [
                item
                for item in values
                if (
                    item.deadline_date
                    or date(
                        item.approximate_year or 9999,
                        item.approximate_month or 1,
                        1,
                    )
                )
                <= until
            ]
        return sorted(
            values,
            key=lambda item: (
                item.deadline_date is None,
                item.deadline_date
                or date(item.approximate_year or 9999, item.approximate_month or 1, 1),
                item.conference_name,
            ),
        )

    async def was_sent(
        self, user_id: int, deadline_id: int, notification_kind: str, threshold: int
    ) -> bool:
        return (
            await self.session.scalar(
                select(Reminder.id).where(
                    Reminder.user_id == user_id,
                    Reminder.deadline_id == deadline_id,
                    Reminder.notification_kind == notification_kind,
                    Reminder.threshold == threshold,
                )
            )
            is not None
        )

    async def record(
        self, user_id: int, deadline_id: int, notification_kind: str, threshold: int
    ) -> None:
        from conference_planner.db.base import utc_now

        await self.record_notice(
            user_id, deadline_id, notification_kind, threshold, utc_now()
        )
        await self.session.commit()
