from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import ModelSelection
from conference_scout.db.repositories.agent_state import AgentStateRepository
from conference_scout.db.repositories.conversations import ConversationRepository
from conference_scout.db.repositories.memories import KnowledgeRelationRepository, MemoryRepository
from conference_scout.db.repositories.pending_actions import PendingActionRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.schemas.common import (
    ConversationStatus,
    KnowledgeRelationKind,
    MemoryKind,
    MemoryScope,
    PendingActionStatus,
)


async def _users(session: AsyncSession) -> tuple[int, int]:
    repository = UserRepository(session)
    first = await repository.get_or_create_by_telegram_id(101)
    second = await repository.get_or_create_by_telegram_id(202)
    await session.flush()
    return first.id, second.id


@pytest.mark.asyncio
async def test_agent_state_is_unique_and_owner_scoped(session: AsyncSession) -> None:
    first_id, second_id = await _users(session)
    repository = AgentStateRepository(session)

    await repository.set_selection(first_id, "ollama-cloud", "gpt-oss:20b")
    await repository.set_selection(first_id, "ollama-cloud", "gemma4:31b")
    await repository.set_selection(second_id, "local", "qwen3")
    await repository.set_capability("ollama-cloud", "gemma4:31b", True)
    await repository.set_capability("ollama-cloud", "gemma4:31b", False)
    await session.commit()

    first = await repository.get_selection(first_id)
    second = await repository.get_selection(second_id)
    capability = await repository.get_capability("ollama-cloud", "gemma4:31b")
    assert first is not None and (first.profile_id, first.model_name) == (
        "ollama-cloud",
        "gemma4:31b",
    )
    assert second is not None and second.model_name == "qwen3"
    assert capability is not None and capability.supports_tools is False

    session.add(ModelSelection(user_id=first_id, profile_id="other", model_name="duplicate"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_conversation_messages_are_ordered_and_owner_scoped(session: AsyncSession) -> None:
    first_id, second_id = await _users(session)
    repository = ConversationRepository(session)
    first = await repository.get_or_create_active(first_id)
    other = await repository.get_or_create_active(second_id)
    await repository.append_message(first_id, first.id, "user", "find conferences")
    await repository.append_message(
        first_id,
        first.id,
        "assistant",
        "Two found",
        domain_references=[{"kind": "conference", "id": 17, "position": 1}],
    )
    await repository.append_message(second_id, other.id, "user", "private")
    await session.commit()

    messages = await repository.list_messages(first_id, first.id)
    assert [(item.role, item.content) for item in messages] == [
        ("user", "find conferences"),
        ("assistant", "Two found"),
    ]
    assert messages[1].domain_references == [{"kind": "conference", "id": 17, "position": 1}]
    assert await repository.list_messages(first_id, other.id) == []

    replacement = await repository.start_new(first_id)
    assert replacement.id != first.id
    assert first.status == ConversationStatus.CLOSED
    assert replacement.status == ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_memories_and_relations_are_owner_scoped_with_provenance(
    session: AsyncSession,
) -> None:
    first_id, second_id = await _users(session)
    memories = MemoryRepository(session)
    relations = KnowledgeRelationRepository(session)
    kept = await memories.create(
        first_id,
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.GLOBAL,
        original_text="Do not suggest events in the USA",
        structured_data={"exclude_country": "US"},
        provenance={"telegram_message_id": 55},
    )
    await memories.create(
        second_id,
        kind=MemoryKind.NOTE,
        scope=MemoryScope.GLOBAL,
        original_text="private",
        structured_data={},
        provenance={"telegram_message_id": 77},
    )
    relation = await relations.create(
        first_id,
        source_type="conference",
        source_id=10,
        relation_type=KnowledgeRelationKind.RELATED_TO,
        target_type="conference",
        target_id=11,
        provenance={"source_url": "https://example.org/evidence"},
    )
    await session.commit()

    assert [item.id for item in await memories.list_active(first_id)] == [kept.id]
    assert (await memories.list_active(first_id))[0].provenance == {"telegram_message_id": 55}
    assert [item.id for item in await relations.list_for_source(first_id, "conference", 10)] == [
        relation.id
    ]
    assert relation.provenance == {"source_url": "https://example.org/evidence"}
    assert await relations.list_for_source(second_id, "conference", 10) == []

    await memories.deactivate(first_id, kept.id)
    assert await memories.list_active(first_id) == []


@pytest.mark.asyncio
async def test_pending_actions_keep_expiry_status_and_owner_scope(session: AsyncSession) -> None:
    first_id, second_id = await _users(session)
    repository = PendingActionRepository(session)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    action = await repository.create(
        first_id,
        action_type="track_conference",
        payload={"conference_id": 17},
        summary="Отслеживать конференцию 17",
        expires_at=expires_at,
    )
    await session.commit()

    loaded = await repository.get_for_user(first_id, action.id)
    assert loaded is not None
    assert loaded.status == PendingActionStatus.PENDING
    assert loaded.expires_at == expires_at
    assert await repository.get_for_user(second_id, action.id) is None

    await repository.set_status(
        first_id, action.id, PendingActionStatus.CANCELLED, error="cancelled by owner"
    )
    await session.commit()
    loaded = await repository.get_for_user(first_id, action.id)
    assert loaded is not None
    assert loaded.status == PendingActionStatus.CANCELLED
    assert loaded.error == "cancelled by owner"
