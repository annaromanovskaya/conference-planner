from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Reminder


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
