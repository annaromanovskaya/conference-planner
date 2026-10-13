from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import ChatMessageRecord, Conversation
from conference_scout.schemas.common import ConversationStatus


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active(self, user_id: int) -> Conversation | None:
        return await self.session.scalar(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.status == ConversationStatus.ACTIVE,
            )
        )

    async def get_or_create_active(self, user_id: int) -> Conversation:
        conversation = await self.get_active(user_id)
        if conversation is None:
            conversation = Conversation(user_id=user_id)
            self.session.add(conversation)
            await self.session.flush()
        return conversation

    async def start_new(self, user_id: int) -> Conversation:
        active = await self.get_active(user_id)
        if active is not None:
            active.status = ConversationStatus.CLOSED
        conversation = Conversation(user_id=user_id)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def append_message(
        self,
        user_id: int,
        conversation_id: int,
        role: str,
        content: str,
        *,
        domain_references: list[dict] | None = None,
    ) -> ChatMessageRecord:
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if conversation is None:
            raise ValueError("conversation not found")
        message = ChatMessageRecord(
            conversation_id=conversation_id,
            role=role,
            content=content,
            domain_references=domain_references or [],
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_messages(self, user_id: int, conversation_id: int) -> list[ChatMessageRecord]:
        return list(
            await self.session.scalars(
                select(ChatMessageRecord)
                .join(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    ChatMessageRecord.conversation_id == conversation_id,
                )
                .order_by(ChatMessageRecord.id)
            )
        )
