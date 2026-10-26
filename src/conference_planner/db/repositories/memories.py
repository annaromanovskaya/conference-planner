from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import KnowledgeRelation, UserMemory
from conference_planner.schemas.common import KnowledgeRelationKind, MemoryKind, MemoryScope


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        *,
        kind: MemoryKind,
        scope: MemoryScope,
        original_text: str,
        structured_data: dict,
        provenance: dict,
        topic_id: int | None = None,
        conference_id: int | None = None,
    ) -> UserMemory:
        memory = UserMemory(
            user_id=user_id,
            kind=kind,
            scope=scope,
            original_text=original_text,
            structured_data=structured_data,
            provenance=provenance,
            topic_id=topic_id,
            conference_id=conference_id,
        )
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def list_active(self, user_id: int) -> list[UserMemory]:
        return list(
            await self.session.scalars(
                select(UserMemory)
                .where(UserMemory.user_id == user_id, UserMemory.active.is_(True))
                .order_by(UserMemory.id)
            )
        )

    async def deactivate(self, user_id: int, memory_id: int) -> UserMemory | None:
        memory = await self.session.scalar(
            select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user_id)
        )
        if memory is not None:
            memory.active = False
            await self.session.flush()
        return memory


class KnowledgeRelationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        *,
        source_type: str,
        source_id: int,
        relation_type: KnowledgeRelationKind,
        target_type: str,
        target_id: int,
        provenance: dict,
    ) -> KnowledgeRelation:
        relation = KnowledgeRelation(
            user_id=user_id,
            source_type=source_type,
            source_id=source_id,
            relation_type=relation_type,
            target_type=target_type,
            target_id=target_id,
            provenance=provenance,
        )
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def list_for_source(
        self, user_id: int, source_type: str, source_id: int
    ) -> list[KnowledgeRelation]:
        return list(
            await self.session.scalars(
                select(KnowledgeRelation)
                .where(
                    KnowledgeRelation.user_id == user_id,
                    KnowledgeRelation.source_type == source_type,
                    KnowledgeRelation.source_id == source_id,
                )
                .order_by(KnowledgeRelation.id)
            )
        )
