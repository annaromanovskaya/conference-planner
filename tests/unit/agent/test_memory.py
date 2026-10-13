import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.agent.memory import MemoryService
from conference_scout.db.repositories.users import UserRepository
from conference_scout.schemas.agent import MemoryProposal
from conference_scout.schemas.common import MemoryKind, MemoryScope


async def _owner_id(session: AsyncSession) -> int:
    owner = await UserRepository(session).get_or_create_by_telegram_id(123)
    await session.flush()
    return owner.id


@pytest.mark.asyncio
async def test_active_conversation_is_reused_until_start_new(session: AsyncSession) -> None:
    owner_id = await _owner_id(session)
    service = MemoryService(session, max_context_chars=100)

    first = await service.get_context(owner_id, "hello")
    reused = await service.get_context(owner_id, "again")
    replacement = await service.start_new_conversation(owner_id)

    assert first.conversation_id == reused.conversation_id
    assert replacement.id != first.conversation_id


@pytest.mark.asyncio
async def test_context_keeps_recent_messages_in_order_with_references(
    session: AsyncSession,
) -> None:
    owner_id = await _owner_id(session)
    service = MemoryService(session, max_context_chars=35)
    await service.append_message(owner_id, "user", "old message that does not fit")
    await service.append_message(owner_id, "user", "show results")
    await service.append_message(
        owner_id,
        "assistant",
        "first and second",
        domain_references=[{"kind": "conference", "id": 9, "position": 1}],
    )

    context = await service.get_context(owner_id, "second")

    assert [(item.role, item.content) for item in context.messages] == [
        ("user", "show results"),
        ("assistant", "first and second"),
    ]
    assert context.messages[-1].domain_references[0].id == 9


@pytest.mark.asyncio
async def test_compaction_summarizes_only_visible_user_and_assistant_text(
    session: AsyncSession,
) -> None:
    owner_id = await _owner_id(session)
    service = MemoryService(session, max_context_chars=20)
    await service.append_message(owner_id, "user", "visible question")
    await service.append_message(owner_id, "tool", "API_KEY=top-secret")
    await service.append_message(owner_id, "assistant", "visible answer")
    captured: list[tuple[str, str]] = []

    async def summarize(previous: str, transcript: str) -> str:
        captured.append((previous, transcript))
        return "User asked a visible question and received an answer."

    changed = await service.compact_conversation(owner_id, summarize)
    context = await service.get_context(owner_id, "question")

    assert changed is True
    assert captured == [("", "user: visible question\nassistant: visible answer")]
    assert "top-secret" not in captured[0][1]
    assert context.summary == "User asked a visible question and received an answer."


@pytest.mark.asyncio
async def test_memory_retrieval_and_conflicts_are_owner_scoped(session: AsyncSession) -> None:
    owner_id = await _owner_id(session)
    other = await UserRepository(session).get_or_create_by_telegram_id(456)
    service = MemoryService(session, max_context_chars=100)
    exclude = MemoryProposal(
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.GLOBAL,
        original_text="Do not suggest events in the USA",
        rule_data={"operation": "exclude", "field": "country", "value": "US"},
        provenance={"telegram_message_id": 1},
    )
    kept = await service.save_memory(owner_id, exclude)
    await service.save_memory(
        other.id,
        exclude.model_copy(update={"original_text": "private other owner rule"}),
    )

    context = await service.get_context(owner_id, "conference in the USA")
    equivalent = await service.detect_conflicts(owner_id, exclude)
    opposite = await service.detect_conflicts(
        owner_id,
        exclude.model_copy(
            update={
                "original_text": "Prefer events in the USA",
                "rule_data": {"operation": "include", "field": "country", "value": "US"},
            }
        ),
    )

    assert [item.id for item in context.memories] == [kept.id]
    assert equivalent.equivalent_ids == (kept.id,)
    assert opposite.conflicting_ids == (kept.id,)
