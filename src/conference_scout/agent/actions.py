from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import PendingAction
from conference_scout.db.repositories.pending_actions import PendingActionRepository
from conference_scout.schemas.agent import (
    ActionPayload,
    ActionResult,
    PendingActionView,
    parse_action_payload,
)
from conference_scout.schemas.common import PendingActionStatus

ActionExecutor = Callable[[int, ActionPayload], Awaitable[str]]


class ActionUnavailable(RuntimeError):
    """Raised when a pending action cannot safely be executed."""


class ConfirmationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        executors: Mapping[str, ActionExecutor],
        ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.executors = dict(executors)
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(UTC))
        self.repository = PendingActionRepository(session)

    async def propose(self, user_id: int, payload: ActionPayload) -> PendingActionView:
        parsed = parse_action_payload(payload.model_dump(mode="json"))
        action = await self.repository.create(
            user_id,
            action_type=parsed.action,
            payload=parsed.model_dump(mode="json"),
            summary=parsed.confirmation_summary(),
            expires_at=self.clock() + self.ttl,
        )
        await self.session.commit()
        return self._view(action)

    async def confirm(self, user_id: int, action_id: int) -> ActionResult:
        action = await self._get_for_update(user_id, action_id)
        if action is None:
            raise ActionUnavailable("action not found")
        self._ensure_available(action)

        if self._as_utc(action.expires_at) <= self._as_utc(self.clock()):
            action.status = PendingActionStatus.EXPIRED
            await self.session.commit()
            raise ActionUnavailable("action expired")

        claimed = await self.session.execute(
            update(PendingAction)
            .where(
                PendingAction.id == action_id,
                PendingAction.user_id == user_id,
                PendingAction.status.in_({PendingActionStatus.PENDING, PendingActionStatus.FAILED}),
            )
            .values(status=PendingActionStatus.EXECUTING, error=None)
        )
        if claimed.rowcount != 1:
            await self.session.rollback()
            current = await self._get_for_update(user_id, action_id)
            if current is None:
                raise ActionUnavailable("action not found")
            self._ensure_available(current)
            raise ActionUnavailable("action is already being processed")
        await self.session.commit()
        action = await self._get_for_update(user_id, action_id)
        if action is None:
            raise ActionUnavailable("action not found")

        try:
            payload = parse_action_payload(action.payload)
            if payload.action != action.action_type:
                raise ValueError("action type does not match payload")
            executor = self.executors[payload.action]
        except (ValidationError, ValueError, KeyError) as exc:
            action.status = PendingActionStatus.FAILED
            action.error = "action payload is invalid"
            await self.session.commit()
            raise ActionUnavailable("action payload is invalid") from exc

        try:
            message = await executor(user_id, payload)
            action.status = PendingActionStatus.EXECUTED
            action.error = None
            await self.session.commit()
        except Exception as exc:
            await self.session.rollback()
            failed = await self._get_for_update(user_id, action_id)
            if failed is not None:
                failed.status = PendingActionStatus.FAILED
                failed.error = "action execution failed"
                await self.session.commit()
            raise ActionUnavailable("action execution failed") from exc

        return ActionResult(action_id=action_id, success=True, message=message)

    async def cancel(self, user_id: int, action_id: int) -> PendingActionView:
        action = await self._get_for_update(user_id, action_id)
        if action is None:
            raise ActionUnavailable("action not found")
        if action.status not in {PendingActionStatus.PENDING, PendingActionStatus.FAILED}:
            self._ensure_available(action)
        action.status = PendingActionStatus.CANCELLED
        action.error = None
        await self.session.commit()
        return self._view(action)

    async def _get_for_update(self, user_id: int, action_id: int) -> PendingAction | None:
        return await self.session.scalar(
            select(PendingAction)
            .where(PendingAction.id == action_id, PendingAction.user_id == user_id)
            .with_for_update()
        )

    @staticmethod
    def _ensure_available(action: PendingAction) -> None:
        messages = {
            PendingActionStatus.EXECUTED: "action already completed",
            PendingActionStatus.EXECUTING: "action is already being processed",
            PendingActionStatus.CANCELLED: "action cancelled",
            PendingActionStatus.EXPIRED: "action expired",
        }
        if action.status in messages:
            raise ActionUnavailable(messages[action.status])

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _view(action: PendingAction) -> PendingActionView:
        return PendingActionView(
            id=action.id,
            action_type=action.action_type,
            summary=action.summary,
            status=action.status,
            expires_at=action.expires_at,
        )
