from conference_scout.ai.models import ChatMessage
from conference_scout.ai.registry import ModelRegistry


class ActiveLLMProvider:
    """Bridge the persisted active model to the structured AIService protocol."""

    def __init__(self, registry: ModelRegistry, user_id: int) -> None:
        self.registry = registry
        self.user_id = user_id

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        active = await self.registry.get_active(self.user_id)
        response = await self.registry.gateway_for(active.profile_id).chat(
            active.model_name,
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
        )
        return response.content
