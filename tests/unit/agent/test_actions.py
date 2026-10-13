import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conference_scout.agent.actions import ActionUnavailable, ConfirmationService
from conference_scout.db.repositories.pending_actions import PendingActionRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.schemas.agent import (
    AddSourceAction,
    AddTopicAction,
    ArchiveConferenceAction,
    ForgetMemoryAction,
    RunDiscoveryAction,
    SaveMemoryAction,
    TrackConferenceAction,
    parse_action_payload,
)
from conference_scout.schemas.common import (
    MemoryKind,
    MemoryScope,
    PendingActionStatus,
    SourceType,
)


async def _users(session: AsyncSession) -> tuple[int, int]:
    repository = UserRepository(session)
    first = await repository.get_or_create_by_telegram_id(101)
    second = await repository.get_or_create_by_telegram_id(202)
    await session.flush()
    return first.id, second.id


@pytest.mark.parametrize(
    ("payload", "summary_part"),
    [
        (
            AddTopicAction(
                name="Astroinformatics",
                description="ML in astronomy",
                keywords=("astroinformatics",),
            ),
            "Добавить тему «Astroinformatics»",
        ),
        (AddSourceAction(source_type=SourceType.DOMAIN, value="iau.org"), "iau.org"),
        (TrackConferenceAction(conference_id=11), "Отслеживать конференцию 11"),
        (ArchiveConferenceAction(conference_id=12), "архив конференцию 12"),
        (
            SaveMemoryAction(
                kind=MemoryKind.PREFERENCE,
                scope=MemoryScope.GLOBAL,
                original_text="No USA events",
                rule_data={"exclude_country": "US"},
                provenance={"telegram_message_id": 1},
            ),
            "Запомнить: No USA events",
        ),
        (ForgetMemoryAction(memory_id=5), "Забыть запись памяти 5"),
        (RunDiscoveryAction(years=(2026, 2027)), "поиск конференций за 2026, 2027"),
    ],
)
def test_action_payloads_round_trip_and_have_deterministic_russian_summary(
    payload, summary_part: str
) -> None:
    parsed = parse_action_payload(payload.model_dump(mode="json"))

    assert type(parsed) is type(payload)
    assert summary_part in parsed.confirmation_summary()


def test_action_payload_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        parse_action_payload({"action": "run_shell", "command": "whoami"})


@pytest.mark.asyncio
async def test_confirmation_executes_validated_action_exactly_once(session: AsyncSession) -> None:
    owner_id, _ = await _users(session)
    calls: list[tuple[int, int]] = []

    async def execute(user_id: int, payload: TrackConferenceAction) -> str:
        calls.append((user_id, payload.conference_id))
        return "tracking enabled"

    service = ConfirmationService(
        session, executors={"track_conference": execute}, ttl=timedelta(minutes=10)
    )
    pending = await service.propose(owner_id, TrackConferenceAction(conference_id=17))

    result = await service.confirm(owner_id, pending.id)
    with pytest.raises(ActionUnavailable, match="already completed"):
        await service.confirm(owner_id, pending.id)

    assert result.success is True
    assert result.message == "tracking enabled"
    assert calls == [(owner_id, 17)]


@pytest.mark.asyncio
async def test_foreign_expired_and_cancelled_actions_never_execute(
    session: AsyncSession,
) -> None:
    owner_id, other_id = await _users(session)
    calls: list[int] = []

    async def execute(_user_id: int, payload: TrackConferenceAction) -> str:
        calls.append(payload.conference_id)
        return "done"

    now = datetime(2026, 10, 13, tzinfo=UTC)
    service = ConfirmationService(
        session,
        executors={"track_conference": execute},
        ttl=timedelta(minutes=5),
        clock=lambda: now,
    )
    foreign = await service.propose(owner_id, TrackConferenceAction(conference_id=1))
    with pytest.raises(ActionUnavailable, match="not found"):
        await service.confirm(other_id, foreign.id)

    cancelled = await service.propose(owner_id, TrackConferenceAction(conference_id=2))
    await service.cancel(owner_id, cancelled.id)
    with pytest.raises(ActionUnavailable, match="cancelled"):
        await service.confirm(owner_id, cancelled.id)

    expired = await service.propose(owner_id, TrackConferenceAction(conference_id=3))
    service.clock = lambda: now + timedelta(minutes=6)
    with pytest.raises(ActionUnavailable, match="expired"):
        await service.confirm(owner_id, expired.id)

    assert calls == []


@pytest.mark.asyncio
async def test_malformed_stored_payload_is_failed_without_execution(
    session: AsyncSession,
) -> None:
    owner_id, _ = await _users(session)
    calls: list[int] = []

    async def execute(_user_id: int, _payload) -> str:
        calls.append(1)
        return "done"

    repository = PendingActionRepository(session)
    stored = await repository.create(
        owner_id,
        action_type="track_conference",
        payload={"action": "track_conference", "conference_id": "invalid"},
        summary="invalid",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await session.commit()
    service = ConfirmationService(session, executors={"track_conference": execute})

    with pytest.raises(ActionUnavailable, match="invalid"):
        await service.confirm(owner_id, stored.id)

    loaded = await repository.get_for_user(owner_id, stored.id)
    assert loaded is not None and loaded.status == PendingActionStatus.FAILED
    assert calls == []


@pytest.mark.asyncio
async def test_executor_failure_rolls_back_and_remains_retryable(session: AsyncSession) -> None:
    owner_id, _ = await _users(session)
    attempts = 0

    async def fail(_user_id: int, _payload) -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database unavailable")

    service = ConfirmationService(session, executors={"track_conference": fail})
    pending = await service.propose(owner_id, TrackConferenceAction(conference_id=17))

    with pytest.raises(ActionUnavailable, match="failed"):
        await service.confirm(owner_id, pending.id)

    loaded = await PendingActionRepository(session).get_for_user(owner_id, pending.id)
    assert loaded is not None and loaded.status == PendingActionStatus.FAILED
    assert loaded.error == "action execution failed"
    assert attempts == 1


@pytest.mark.asyncio
async def test_claimed_action_cannot_be_executed_by_duplicate_callback(
    session: AsyncSession,
) -> None:
    owner_id, _ = await _users(session)
    calls = []

    async def execute(_user_id: int, _payload) -> str:
        calls.append(1)
        return "done"

    repository = PendingActionRepository(session)
    action = await repository.create(
        owner_id,
        action_type="track_conference",
        payload={"action": "track_conference", "conference_id": 1},
        summary="tracking",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    action.status = PendingActionStatus.EXECUTING
    await session.commit()

    with pytest.raises(ActionUnavailable, match="already being processed"):
        await ConfirmationService(session, executors={"track_conference": execute}).confirm(
            owner_id, action.id
        )

    assert calls == []


@pytest.mark.asyncio
async def test_expired_execution_lease_is_recovered(session: AsyncSession) -> None:
    owner_id, _ = await _users(session)
    now = datetime(2026, 10, 13, tzinfo=UTC)
    action = await PendingActionRepository(session).create(
        owner_id,
        action_type="track_conference",
        payload={"action": "track_conference", "conference_id": 1},
        summary="tracking",
        expires_at=now - timedelta(seconds=1),
    )
    action.status = PendingActionStatus.EXECUTING
    await session.commit()

    service = ConfirmationService(
        session,
        executors={"track_conference": lambda _user_id, _payload: None},
        clock=lambda: now,
    )
    with pytest.raises(ActionUnavailable, match="expired"):
        await service.confirm(owner_id, action.id)

    loaded = await PendingActionRepository(session).get_for_user(owner_id, action.id)
    assert loaded is not None and loaded.status == PendingActionStatus.EXPIRED


@pytest.mark.asyncio
async def test_cancelled_executor_releases_claim_for_retry(session: AsyncSession) -> None:
    owner_id, _ = await _users(session)

    async def cancel(_user_id: int, _payload) -> str:
        raise asyncio.CancelledError

    service = ConfirmationService(session, executors={"track_conference": cancel})
    pending = await service.propose(owner_id, TrackConferenceAction(conference_id=17))

    with pytest.raises(asyncio.CancelledError):
        await service.confirm(owner_id, pending.id)

    loaded = await PendingActionRepository(session).get_for_user(owner_id, pending.id)
    assert loaded is not None and loaded.status == PendingActionStatus.FAILED


@pytest.mark.asyncio
async def test_concurrent_confirmations_claim_action_once(session: AsyncSession) -> None:
    owner_id, _ = await _users(session)
    action = await PendingActionRepository(session).create(
        owner_id,
        action_type="track_conference",
        payload={"action": "track_conference", "conference_id": 1},
        summary="tracking",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await session.commit()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def execute(_user_id: int, _payload) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "done"

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as first, factory() as duplicate:
        first_service = ConfirmationService(first, executors={"track_conference": execute})
        duplicate_service = ConfirmationService(duplicate, executors={"track_conference": execute})
        first_task = asyncio.create_task(first_service.confirm(owner_id, action.id))
        await started.wait()
        with pytest.raises(ActionUnavailable, match="already being processed"):
            await duplicate_service.confirm(owner_id, action.id)
        release.set()
        await first_task

    assert calls == 1
