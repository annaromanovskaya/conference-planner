from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject


def private_instance_message() -> str:
    return "Это приватный экземпляр Conference Scout."


class OwnerOnlyFilter(BaseFilter):
    def __init__(self, owner_telegram_id: int) -> None:
        self.owner_telegram_id = owner_telegram_id

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id == self.owner_telegram_id


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_telegram_id: int) -> None:
        self.owner_telegram_id = owner_telegram_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None and user.id == self.owner_telegram_id:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(private_instance_message())
        elif isinstance(event, CallbackQuery):
            await event.answer(private_instance_message(), show_alert=True)
        return None
