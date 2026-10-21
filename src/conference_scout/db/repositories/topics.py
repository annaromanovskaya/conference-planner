from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import Topic
from conference_scout.schemas.topics import TopicNormalization


class TopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: int) -> list[Topic]:
        return list(
            await self.session.scalars(
                select(Topic).where(Topic.user_id == user_id).order_by(Topic.name)
            )
        )

    async def create(self, user_id: int, value: TopicNormalization) -> Topic:
        topic = Topic(
            user_id=user_id,
            name=value.name,
            description=value.description,
            keywords=value.keywords,
            enabled=True,
        )
        self.session.add(topic)
        await self.session.flush()
        return topic

    async def set_enabled(self, topic_id: int, enabled: bool) -> None:
        topic = await self.session.get(Topic, topic_id)
        if topic is not None:
            topic.enabled = enabled

    async def delete(self, topic_id: int) -> None:
        topic = await self.session.get(Topic, topic_id)
        if topic is not None:
            await self.session.delete(topic)
