import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from conference_planner.agent.actions import ActionUnavailable
from conference_planner.ai.gateway import ModelGatewayError
from conference_planner.ai.service import AIResponseError
from conference_planner.bot.keyboards import main_menu

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
            state = data.get("state")
            if state is not None:
                await state.clear()
            logger.warning("LLM request failed: %s", type(exc).__name__)
            text = "Модель или действие сейчас недоступны. Попробуйте ещё раз или выберите /model."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message) or hasattr(event, "answer"):
                await event.answer(text, reply_markup=main_menu())
            return None
