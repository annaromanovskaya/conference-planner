from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import PendingAction
from conference_planner.schemas.common import PendingActionStatus


class PendingActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        *,
        action_type: str,
        payload: dict,
        summary: str,
        expires_at: datetime,
    ) -> PendingAction:
        action = PendingAction(
            user_id=user_id,
            action_type=action_type,
            payload=payload,
            summary=summary,
            expires_at=expires_at,
        )
        self.session.add(action)
        await self.session.flush()
        return action

    async def get_for_user(self, user_id: int, action_id: int) -> PendingAction | None:
        return await self.session.scalar(
            select(PendingAction).where(
                PendingAction.id == action_id,
                PendingAction.user_id == user_id,
            )
        )

    async def set_status(
        self,
        user_id: int,
        action_id: int,
        status: PendingActionStatus,
        *,
        error: str | None = None,
    ) -> PendingAction | None:
        action = await self.get_for_user(user_id, action_id)
        if action is not None:
            action.status = status
            action.error = error
            await self.session.flush()
        return action
