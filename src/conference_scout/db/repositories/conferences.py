from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Deadline, DeadlineHistory


class ConferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
