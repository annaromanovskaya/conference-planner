from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from conference_planner.ai.models import ChatMessage
from conference_planner.ai.registry import ModelRegistry
from conference_planner.schemas.common import ModelRole


@dataclass(frozen=True, slots=True)
class ModelCallMetadata:
    profile_id: str
    requested_model_name: str
    model_name: str | None


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
        self._last_call: ContextVar[ModelCallMetadata | None] = ContextVar(
            f"last_model_call_{id(self)}",
            default=None,
        )

    @property
    def last_call(self) -> ModelCallMetadata | None:
        return self._last_call.get()

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
        self._last_call.set(
            ModelCallMetadata(
                profile_id=active.profile_id,
                requested_model_name=active.model_name,
                model_name=response.model_name,
            )
        )
        return response.content
