from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
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
async def test_conference_match_recognizes_child_page_of_event_site(
    session: AsyncSession,
) -> None:
    conference = Conference(
        name="IMBASE 2027",
        normalized_name="imbase 2027",
        website="https://meetings.iac.es/imbase2027/",
        source_url="https://meetings.iac.es/imbase2027/",
    )
    session.add(conference)
    await session.flush()

    matched = await ConferenceRepository(session).find_match(
        "The Impact of Binaries on Stellar Evolution",
        {"https://meetings.iac.es/imbase2027/pages/important-dates.php"},
    )

    assert matched is not None
    assert matched.id == conference.id


@pytest.mark.asyncio
async def test_conference_match_does_not_treat_domain_root_as_event_scope(
    session: AsyncSession,
) -> None:
    conference = Conference(
        name="First IAU Conference",
        normalized_name="first iau conference",
        website="https://www.iau.org/",
    )
    session.add(conference)
    await session.flush()

    matched = await ConferenceRepository(session).find_match(
        "Second IAU Conference",
        {"https://www.iau.org/science/meetings/second"},
    )

    assert matched is None


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
    await repository.record_notice(owner.id, deadline.id, "EXACT", 14, datetime.now(UTC))
    await session.commit()

    with pytest.raises(IntegrityError):
        await repository.record_notice(owner.id, deadline.id, "EXACT", 14, datetime.now(UTC))


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

    subscription = await session.scalar(select(Subscription))
    assert subscription is not None
    subscription.status = ConferenceStatus.ARCHIVED
    await session.commit()
    assert await ReminderRepository(session).candidates(owner.id) == []


@pytest.mark.asyncio
async def test_deadline_projection_and_reminders_include_exact_and_approximate(
    session: AsyncSession,
) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(999)
    conference = Conference(name="IMBASE", normalized_name="imbase")
    session.add(conference)
    await session.flush()
    session.add(Subscription(user_id=owner.id, conference_id=conference.id))
    session.add_all(
        [
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.REGISTRATION,
                approximate_year=2027,
                approximate_month=5,
                tentative=True,
                source_url="https://example.org/dates",
            ),
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
                deadline_date=date(2027, 2, 12),
                source_url="https://example.org/dates",
            ),
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.PAYMENT,
                approximate_year=2025,
                approximate_month=1,
                source_url="https://example.org/old",
            ),
            Deadline(
                conference_id=conference.id,
                deadline_type=DeadlineType.OTHER,
                approximate_year=2026,
                approximate_month=9,
                source_url="https://example.org/past-month",
            ),
        ]
    )
    await session.commit()
    repository = ReminderRepository(session)

    upcoming = await repository.upcoming(owner.id, date(2026, 10, 14))
    reminders = await repository.candidates(owner.id)

    assert [item.deadline_type for item in upcoming] == [
        "ABSTRACT_SUBMISSION",
        "REGISTRATION",
    ]
    assert [item.deadline_type for item in reminders] == [
        "REGISTRATION",
        "ABSTRACT_SUBMISSION",
        "PAYMENT",
        "OTHER",
    ]
    assert reminders[0].approximate_month == 5


@pytest.mark.asyncio
async def test_tracking_creates_owner_subscription(session: AsyncSession) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(123)
    conference = Conference(name="New", normalized_name="new")
    session.add(conference)
    await session.flush()

    await ConferenceRepository(session).track(owner.id, conference.id)
    await session.commit()

    subscription = await session.scalar(select(Subscription))
    assert subscription is not None
    assert subscription.status == ConferenceStatus.TRACKING
    assert conference.status == ConferenceStatus.DISCOVERED


@pytest.mark.asyncio
async def test_archive_is_owner_scoped_and_does_not_mutate_conference(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    first = await users.get_or_create_by_telegram_id(123)
    second = await users.get_or_create_by_telegram_id(456)
    conference = Conference(name="Shared", normalized_name="shared")
    session.add(conference)
    await session.flush()
    repository = ConferenceRepository(session)
    await repository.track(first.id, conference.id)
    await repository.track(second.id, conference.id)
    await repository.archive(first.id, conference.id)
    await session.commit()

    assert [
        item.id
        for item in await repository.list_accessible_by_status(first.id, ConferenceStatus.ARCHIVED)
    ] == [conference.id]
    assert await repository.list_accessible_by_status(first.id, ConferenceStatus.TRACKING) == []
    assert [
        item.id
        for item in await repository.list_accessible_by_status(second.id, ConferenceStatus.TRACKING)
    ] == [conference.id]
    assert conference.status == ConferenceStatus.DISCOVERED
