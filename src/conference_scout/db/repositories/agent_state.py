from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import ModelCapability, ModelSelection


class AgentStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_selection(self, user_id: int) -> ModelSelection | None:
        return await self.session.scalar(
            select(ModelSelection).where(ModelSelection.user_id == user_id)
        )

    async def set_selection(self, user_id: int, profile_id: str, model_name: str) -> ModelSelection:
        selection = await self.get_selection(user_id)
        if selection is None:
            selection = ModelSelection(
                user_id=user_id, profile_id=profile_id, model_name=model_name
            )
            self.session.add(selection)
        else:
            selection.profile_id = profile_id
            selection.model_name = model_name
        await self.session.flush()
        return selection

    async def get_capability(self, profile_id: str, model_name: str) -> ModelCapability | None:
        return await self.session.scalar(
            select(ModelCapability).where(
                ModelCapability.profile_id == profile_id,
                ModelCapability.model_name == model_name,
            )
        )

    async def set_capability(
        self, profile_id: str, model_name: str, supports_tools: bool
    ) -> ModelCapability:
        capability = await self.get_capability(profile_id, model_name)
        if capability is None:
            capability = ModelCapability(
                profile_id=profile_id,
                model_name=model_name,
                supports_tools=supports_tools,
            )
            self.session.add(capability)
        else:
            capability.supports_tools = supports_tools
            capability.checked_at = datetime.now(UTC)
        await self.session.flush()
        return capability
