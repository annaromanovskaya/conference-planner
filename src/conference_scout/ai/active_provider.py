from typing import Any

from conference_scout.ai.models import ChatMessage
from conference_scout.ai.registry import ModelRegistry
from conference_scout.schemas.common import ModelRole


class ActiveLLMProvider:
    """Bridge the persisted active model to the structured AIService protocol."""

    def __init__(
        self,
        registry: ModelRegistry,
        user_id: int,
        role: ModelRole | None = None,
    ) -> None:
        self.registry = registry
        self.user_id = user_id
        self.role = role

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        active = (
            await self.registry.get_for_role(self.user_id, self.role)
            if self.role is not None
            else await self.registry.get_active(self.user_id)
        )
        response = await self.registry.chat(
            self.user_id,
            active,
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            response_schema=response_schema,
        )
        return response.content
