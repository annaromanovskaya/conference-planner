import json
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.agent.prompts import serialize_memory_views
from conference_scout.db.models import UserMemory
from conference_scout.db.repositories.conversations import ConversationRepository
from conference_scout.db.repositories.memories import MemoryRepository
from conference_scout.schemas.agent import (
    ConversationMessage,
    DomainReference,
    MemoryConflict,
    MemoryContext,
    MemoryProposal,
    MemoryView,
)

Summarizer = Callable[[str, str], Awaitable[str]]


class MemoryService:
    def __init__(self, session: AsyncSession, *, max_context_chars: int) -> None:
        self.session = session
        self.max_context_chars = max_context_chars
        self.conversations = ConversationRepository(session)
        self.memories = MemoryRepository(session)

    async def get_context(self, user_id: int, query: str) -> MemoryContext:
        conversation = await self.conversations.get_or_create_active(user_id)
        records = await self.conversations.list_messages(user_id, conversation.id)
        selected = []
        used = 0
        message_budget = max(0, self.max_context_chars - len(conversation.summary) - 2)
        for record in reversed(records):
            size = len(record.content)
            if selected and used + size > message_budget:
                break
            if not selected or used + size <= message_budget:
                selected.append(record)
                used += size
        selected.reverse()
        messages = tuple(
            ConversationMessage(
                role=record.role,
                content=record.content,
                domain_references=tuple(
                    DomainReference.model_validate(item) for item in record.domain_references
                ),
            )
            for record in selected
        )
        memory_budget = max(0, self.max_context_chars - len(conversation.summary) - used)
        memory_views = []
        relevant = [
            memory
            for memory in await self.memories.list_active(user_id)
            if self._is_relevant(memory, query)
        ]
        priority = {
            "INSTRUCTION": 0,
            "PREFERENCE": 1,
            "NOTE": 2,
        }
        relevant.sort(key=lambda item: (priority[item.kind.value], -item.id))
        for memory in relevant:
            view = self._view(memory)
            candidate = [*memory_views, view]
            if len(serialize_memory_views(candidate)) > memory_budget:
                continue
            memory_views.append(view)
        return MemoryContext(
            conversation_id=conversation.id,
            summary=conversation.summary,
            messages=messages,
            memories=tuple(memory_views),
        )

    async def append_message(
        self,
        user_id: int,
        role: str,
        content: str,
        *,
        domain_references: list[dict] | None = None,
    ) -> None:
        conversation = await self.conversations.get_or_create_active(user_id)
        await self.conversations.append_message(
            user_id,
            conversation.id,
            role,
            content,
            domain_references=domain_references,
        )
        await self.session.flush()

    async def start_new_conversation(self, user_id: int):
        conversation = await self.conversations.start_new(user_id)
        await self.session.flush()
        return conversation

    async def compact_conversation(self, user_id: int, summarizer: Summarizer) -> bool:
        conversation = await self.conversations.get_or_create_active(user_id)
        records = await self.conversations.list_messages(user_id, conversation.id)
        visible = [record for record in records if record.role in {"user", "assistant"}]
        if sum(len(record.content) for record in visible) <= self.max_context_chars:
            return False
        transcript = "\n".join(f"{record.role}: {record.content}" for record in visible)
        summary = await summarizer(conversation.summary, transcript)
        summary_limit = max(1, self.max_context_chars // 2)
        conversation.summary = summary[-summary_limit:]
        references: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for record in records:
            for reference in record.domain_references:
                key = (str(reference.get("kind")), int(reference.get("id", 0)))
                if key not in seen:
                    seen.add(key)
                    references.append(reference)
        await self.conversations.replace_messages_with_reference_marker(
            user_id, conversation.id, references
        )
        await self.session.flush()
        return True

    async def list_memories(self, user_id: int) -> tuple[MemoryView, ...]:
        return tuple(self._view(item) for item in await self.memories.list_active(user_id))

    async def save_memory(self, user_id: int, proposal: MemoryProposal) -> MemoryView:
        memory = await self.memories.create(
            user_id,
            kind=proposal.kind,
            scope=proposal.scope,
            original_text=proposal.original_text,
            structured_data=proposal.rule_data,
            provenance=proposal.provenance,
            topic_id=proposal.topic_id,
            conference_id=proposal.conference_id,
        )
        await self.session.flush()
        return self._view(memory)

    async def detect_conflicts(self, user_id: int, proposal: MemoryProposal) -> MemoryConflict:
        equivalent: list[int] = []
        conflicting: list[int] = []
        for memory in await self.memories.list_active(user_id):
            if not self._same_scope(memory, proposal):
                continue
            if memory.structured_data == proposal.rule_data:
                equivalent.append(memory.id)
            elif self._opposite_rule(memory.structured_data, proposal.rule_data):
                conflicting.append(memory.id)
        return MemoryConflict(equivalent_ids=tuple(equivalent), conflicting_ids=tuple(conflicting))

    @staticmethod
    def _view(memory: UserMemory) -> MemoryView:
        return MemoryView(
            id=memory.id,
            kind=memory.kind,
            scope=memory.scope,
            original_text=memory.original_text,
            rule_data=memory.structured_data,
            topic_id=memory.topic_id,
            conference_id=memory.conference_id,
            provenance=memory.provenance,
        )

    @staticmethod
    def _is_relevant(memory: UserMemory, query: str) -> bool:
        if memory.scope.value == "GLOBAL":
            return True
        haystack = f"{memory.original_text} {json.dumps(memory.structured_data)}".casefold()
        return any(token in haystack for token in query.casefold().split())

    @staticmethod
    def _same_scope(memory: UserMemory, proposal: MemoryProposal) -> bool:
        return (
            memory.kind == proposal.kind
            and memory.scope == proposal.scope
            and memory.topic_id == proposal.topic_id
            and memory.conference_id == proposal.conference_id
        )

    @staticmethod
    def _opposite_rule(current: dict, proposed: dict) -> bool:
        return (
            current.get("field") == proposed.get("field")
            and current.get("value") == proposed.get("value")
            and current.get("operation") is not None
            and proposed.get("operation") is not None
            and current.get("operation") != proposed.get("operation")
        )
