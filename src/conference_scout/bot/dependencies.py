from collections.abc import Awaitable, Callable
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
        self, session_factory: async_sessionmaker[AsyncSession], owner_user_id: int
    ) -> None:
        self.session_factory = session_factory
        self.owner_user_id = owner_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            for key, repository in (
                ("topic_repository", TopicRepository(session)),
                ("source_repository", SourceRepository(session)),
                ("conference_repository", ConferenceRepository(session)),
                ("reminder_repository", ReminderRepository(session)),
            ):
                repository.owner_user_id = self.owner_user_id
                data[key] = repository
            return await handler(event, data)
