from aiogram import F, Router
from aiogram.types import CallbackQuery

from conference_planner.agent.actions import ActionUnavailable, ConfirmationService
from conference_planner.bot.conference_router import send_next_candidate
from conference_planner.schemas.agent import ImportConferenceCandidateAction, SaveMemoryAction


def build_confirmation_router() -> Router:
    router = Router(name="action-confirmation")

    @router.callback_query(F.data.startswith("action:confirm:"))
    async def confirm_callback(
        callback: CallbackQuery,
        confirmation_service,
        conference_research_repository,
        conference_research_service,
        owner_user_id: int,
    ) -> None:
        await confirm_action(
            callback,
            confirmation_service,
            owner_user_id=owner_user_id,
            conference_research_repository=conference_research_repository,
            conference_research_service=conference_research_service,
        )

    @router.callback_query(F.data.startswith("action:cancel:"))
    async def cancel_callback(
        callback: CallbackQuery, confirmation_service, owner_user_id: int
    ) -> None:
        await cancel_action(callback, confirmation_service, owner_user_id=owner_user_id)

    return router


def _action_id(data: str | None, expected: str) -> int | None:
    prefix = f"action:{expected}:"
    if data is None or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :]
    if not raw.isdigit() or not 0 < len(raw) <= 18:
        return None
    value = int(raw)
    return value if value > 0 else None


async def confirm_action(
    callback: CallbackQuery,
    service: ConfirmationService,
    *,
    owner_user_id: int,
    conference_research_repository=None,
    conference_research_service=None,
) -> None:
    action_id = _action_id(callback.data, "confirm")
    if action_id is None:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    payload = None
    if hasattr(service, "get_payload"):
        payload = await service.get_payload(owner_user_id, action_id)
    try:
        result = await service.confirm(owner_user_id, action_id)
    except ActionUnavailable as exc:
        messages = {
            "action already completed": "Это действие уже выполнено.",
            "action expired": "Срок подтверждения истёк.",
            "action cancelled": "Действие уже отменено.",
            "action not found": "Действие не найдено.",
            "action execution failed": (
                "Не удалось проверить или сохранить кандидата. Он остался в очереди; "
                "попробуйте ещё раз позже."
                if isinstance(payload, ImportConferenceCandidateAction)
                else (
                    "Не удалось сохранить запись в долговременную память. "
                    "Ничего не изменено; попробуйте ещё раз позже."
                    if isinstance(payload, SaveMemoryAction)
                    else "Не удалось выполнить действие. Попробуйте ещё раз позже."
                )
            ),
        }
        await callback.answer(messages.get(str(exc), "Действие недоступно."), show_alert=True)
        return
    await callback.message.answer(f"✅ {result.message}")
    await callback.answer("Выполнено")
    if (
        isinstance(payload, ImportConferenceCandidateAction)
        and conference_research_repository is not None
    ):
        await send_next_candidate(
            callback.message,
            conference_research_repository,
            owner_user_id,
            research_service=conference_research_service,
        )


async def cancel_action(
    callback: CallbackQuery,
    service: ConfirmationService,
    *,
    owner_user_id: int,
) -> None:
    action_id = _action_id(callback.data, "cancel")
    if action_id is None:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    try:
        await service.cancel(owner_user_id, action_id)
    except ActionUnavailable:
        await callback.answer("Действие уже недоступно.", show_alert=True)
        return
    await callback.answer("Действие отменено")
