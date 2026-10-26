from dataclasses import dataclass
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.conferences.deduplication import normalize_name
from conference_planner.db.models import (
    Conference,
    Deadline,
    Subscription,
    TestReminderTask,
)
from conference_planner.schemas.common import (
    ConferenceStatus,
    DeadlineType,
    EvidenceSourceClass,
    TestReminderStatus,
    VerificationStatus,
)


@dataclass(frozen=True, slots=True)
class TestReminderRecord:
    task_id: int
    user_id: int
    conference_name: str
    deadline_id: int
    deadline_type: DeadlineType
    deadline_date: date


class TestReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        deadline_type: DeadlineType,
        deadline_date: date,
        send_at: datetime,
    ) -> TestReminderTask:
        if await self.active_for(user_id) is not None:
            raise ValueError("active test reminder already exists")
        token = uuid4().hex
        source_url = f"https://test.invalid/{token}"
        conference = Conference(
            name=name,
            normalized_name=normalize_name(name),
            website=source_url,
            source_url=source_url,
            status=ConferenceStatus.DISCOVERED,
            is_test=True,
        )
        self.session.add(conference)
        await self.session.flush()
        subscription = Subscription(
            user_id=user_id,
            conference_id=conference.id,
            status=ConferenceStatus.TRACKING,
        )
        deadline = Deadline(
            conference_id=conference.id,
            deadline_type=deadline_type,
            deadline_date=deadline_date,
            tentative=False,
            source_url=source_url,
            verified=True,
            verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
            source_class=EvidenceSourceClass.OFFICIAL_CONFERENCE,
            evidence_excerpt="Тестовый дедлайн, созданный владельцем бота.",
        )
        self.session.add_all((subscription, deadline))
        await self.session.flush()
        task = TestReminderTask(
            user_id=user_id,
            conference_id=conference.id,
            deadline_id=deadline.id,
            send_at=send_at,
            status=TestReminderStatus.PENDING,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def active_for(self, user_id: int) -> TestReminderTask | None:
        return await self.session.scalar(
            select(TestReminderTask).where(TestReminderTask.user_id == user_id)
        )

    async def due(self, now: datetime) -> list[TestReminderTask]:
        return list(
            await self.session.scalars(
                select(TestReminderTask)
                .where(
                    TestReminderTask.status == TestReminderStatus.PENDING,
                    TestReminderTask.send_at <= now,
                )
                .order_by(TestReminderTask.send_at, TestReminderTask.id)
            )
        )

    async def record(self, task: TestReminderTask) -> TestReminderRecord:
        conference = await self.session.get(Conference, task.conference_id)
        deadline = await self.session.get(Deadline, task.deadline_id)
        if conference is None or not conference.is_test:
            raise ValueError("conference is not a test conference")
        if deadline is None or deadline.deadline_date is None:
            raise ValueError("test reminder deadline is invalid")
        return TestReminderRecord(
            task_id=task.id,
            user_id=task.user_id,
            conference_name=conference.name,
            deadline_id=deadline.id,
            deadline_type=deadline.deadline_type,
            deadline_date=deadline.deadline_date,
        )

    async def record_failure(self, task_id: int, error: str) -> TestReminderTask:
        task = await self._require(task_id)
        task.attempt_count += 1
        task.last_error = error[:2_000]
        if task.attempt_count >= 3:
            task.status = TestReminderStatus.FAILED
        await self.session.flush()
        return task

    async def cleanup(self, user_id: int, task_id: int) -> None:
        task = await self._require(task_id)
        if task.user_id != user_id:
            raise ValueError("test reminder not found")
        conference = await self.session.get(Conference, task.conference_id)
        deadline = await self.session.get(Deadline, task.deadline_id)
        if conference is None or not conference.is_test:
            raise ValueError("conference is not a test conference")
        if deadline is None or deadline.conference_id != conference.id:
            raise ValueError("test reminder links are invalid")
        await self.session.delete(task)
        await self.session.flush()
        await self.session.delete(conference)
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()

    async def _require(self, task_id: int) -> TestReminderTask:
        task = await self.session.get(TestReminderTask, task_id)
        if task is None:
            raise ValueError("test reminder not found")
        return task
