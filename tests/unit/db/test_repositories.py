from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Conference, Deadline, Subscription
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.db.repositories.reminders import ReminderRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.owner import bootstrap_owner
from conference_scout.schemas.common import ConferenceStatus, DeadlineType


@pytest.mark.asyncio
async def test_owner_is_resolved_by_telegram_id(session: AsyncSession) -> None:
    first = await bootstrap_owner(session, 12345)
    second = await bootstrap_owner(session, 12345)

    assert first == second
    assert first.telegram_id == 12345


@pytest.mark.asyncio
async def test_user_repository_does_not_create_duplicate(session: AsyncSession) -> None:
    repository = UserRepository(session)
    await repository.get_or_create_by_telegram_id(777)
    await repository.get_or_create_by_telegram_id(777)
    await session.commit()

    assert len(await repository.list_all()) == 1


@pytest.mark.asyncio
async def test_deadline_change_updates_value_and_creates_history(session: AsyncSession) -> None:
    conference = Conference(
        name="Astroinformatics 2027",
        normalized_name="astroinformatics 2027",
        status=ConferenceStatus.TRACKING,
    )
    session.add(conference)
    await session.flush()
    deadline = Deadline(
        conference_id=conference.id,
        deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
        deadline_date=date(2027, 2, 12),
        source_url="https://event.example/dates",
        verified=True,
        last_checked_at=datetime.now(UTC),
    )
    session.add(deadline)
    await session.commit()

    history = await ConferenceRepository(session).update_deadline(
        deadline,
        date(2027, 2, 19),
        "https://event.example/dates",
        verified=True,
    )
    await session.commit()

    assert deadline.deadline_date == date(2027, 2, 19)
    assert history is not None
    assert history.old_date == date(2027, 2, 12)
    assert history.new_date == date(2027, 2, 19)


@pytest.mark.asyncio
async def test_unchanged_deadline_does_not_create_history(session: AsyncSession) -> None:
    conference = Conference(name="A", normalized_name="a")
    session.add(conference)
    await session.flush()
    deadline = Deadline(
        conference_id=conference.id,
        deadline_type=DeadlineType.REGISTRATION,
        deadline_date=date(2027, 1, 1),
        source_url="https://event.example",
        verified=False,
        last_checked_at=datetime.now(UTC),
    )
    session.add(deadline)
    await session.flush()

    history = await ConferenceRepository(session).update_deadline(
        deadline, date(2027, 1, 1), "https://event.example", verified=False
    )

    assert history is None


@pytest.mark.asyncio
async def test_reminder_uniqueness_is_database_enforced(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(123)
    conference = Conference(name="A", normalized_name="a")
    session.add(conference)
    await session.flush()
    deadline = Deadline(
        conference_id=conference.id,
        deadline_type=DeadlineType.OTHER,
        deadline_date=date(2027, 1, 1),
        source_url="https://event.example",
        verified=False,
        last_checked_at=datetime.now(UTC),
    )
    session.add(deadline)
    await session.flush()
    repository = ReminderRepository(session)
    await repository.record_sent(owner.id, deadline.id, 14, datetime.now(UTC))
    await session.commit()

    with pytest.raises(IntegrityError):
        await repository.record_sent(owner.id, deadline.id, 14, datetime.now(UTC))


@pytest.mark.asyncio
async def test_reminder_candidates_only_include_owner_tracking(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(123)
    conference = Conference(name="Tracked", normalized_name="tracked")
    session.add(conference)
    await session.flush()
    session.add(Subscription(user_id=owner.id, conference_id=conference.id))
    deadline = Deadline(
        conference_id=conference.id,
        deadline_type=DeadlineType.REGISTRATION,
        deadline_date=date(2027, 3, 1),
        source_url="https://event.example",
        verified=True,
        last_checked_at=datetime.now(UTC),
    )
    session.add(deadline)
    await session.commit()

    candidates = await ReminderRepository(session).candidates(owner.id)

    assert len(candidates) == 1
    assert candidates[0].conference_name == "Tracked"
