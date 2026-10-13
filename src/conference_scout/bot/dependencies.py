from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.db.repositories.reminders import ReminderRepository
from conference_scout.db.repositories.sources import SourceRepository
from conference_scout.db.repositories.topics import TopicRepository


class RepositoryMiddleware(BaseMiddleware):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        owner_user_id: int,
        service_factory: Callable[[AsyncSession], Mapping[str, Any]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.owner_user_id = owner_user_id
        self.service_factory = service_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["owner_user_id"] = self.owner_user_id
            for key, repository in (
                ("topic_repository", TopicRepository(session)),
                ("source_repository", SourceRepository(session)),
                ("conference_repository", ConferenceRepository(session)),
                ("reminder_repository", ReminderRepository(session)),
            ):
                repository.owner_user_id = self.owner_user_id
                data[key] = repository
            if self.service_factory is not None:
                data.update(self.service_factory(session))
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except BaseException:
                await session.rollback()
                raise
