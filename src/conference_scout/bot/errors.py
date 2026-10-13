import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from conference_scout.agent.actions import ActionUnavailable
from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.service import AIResponseError

logger = logging.getLogger(__name__)


class ExternalErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except (ModelGatewayError, AIResponseError, ActionUnavailable) as exc:
            logger.warning("LLM request failed", extra={"error": type(exc).__name__})
            text = "Модель или действие сейчас недоступны. Попробуйте ещё раз или выберите /model."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message) or hasattr(event, "answer"):
                await event.answer(text)
            return None
