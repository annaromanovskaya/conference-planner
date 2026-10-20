from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from conference_scout.ai.registry import ModelRegistry
from conference_scout.ai.usage import OllamaUsageService, OllamaUsageSnapshot
from conference_scout.bot.keyboards import ollama_usage_keyboard
from conference_scout.bot.model_router import list_model_profiles
from conference_scout.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_scout.runtime_clock import ApplicationClock
from conference_scout.schemas.common import ProviderUsageStatus, WorkflowKind

STATUS_LABELS = {
    ProviderUsageStatus.AVAILABLE: "доступен",
    ProviderUsageStatus.RATE_LIMITED: "временно ограничен",
    ProviderUsageStatus.FREE_EXHAUSTED: "Free usage исчерпан",
    ProviderUsageStatus.AUTH_ERROR: "ошибка авторизации",
    ProviderUsageStatus.UNKNOWN: "неизвестно",
}

RUN_STATUS_LABELS = {
    "PLANNED": "запланирован",
    "RUNNING": "выполняется",
    "PARTIAL": "требует продолжения",
    "COMPLETED": "завершён",
    "FAILED": "завершён с ошибкой",
    "CANCELLED": "отменён",
}


@dataclass(frozen=True, slots=True)
class CycleScheduleView:
    last_run_id: int | None
    last_status: str | None
    last_finished_at: datetime | None
    next_run_at: datetime


class CycleScheduleService:
    def __init__(
        self,
        repository: AgentWorkflowRepository,
        clock: ApplicationClock,
        discovery_hour: int,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.discovery_hour = discovery_hour

    async def snapshot(self, user_id: int) -> CycleScheduleView:
        now = self.clock.now()
        days_until_monday = (7 - now.weekday()) % 7
        next_run = (now + timedelta(days=days_until_monday)).replace(
            hour=self.discovery_hour, minute=0, second=0, microsecond=0
        )
        if next_run <= now:
            next_run += timedelta(days=7)
        last = await self.repository.latest_run(user_id, WorkflowKind.CONFERENCE_CYCLE)
        last_time = last.updated_at if last is not None else None
        if last_time is not None:
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=UTC)
            last_time = last_time.astimezone(self.clock.timezone)
        return CycleScheduleView(
            last_run_id=last.id if last is not None else None,
            last_status=last.status.value if last is not None else None,
            last_finished_at=last_time,
            next_run_at=next_run,
        )


def build_usage_router() -> Router:
    router = Router(name="ollama-usage")

    @router.message(F.text == "Настройки")
    async def settings(
        message: Message,
        state: FSMContext,
        ollama_usage_service: OllamaUsageService,
        application_clock: ApplicationClock,
        conference_schedule_service: CycleScheduleService,
        owner_user_id: int,
    ) -> None:
        await show_usage(
            message,
            state,
            ollama_usage_service,
            owner_user_id=owner_user_id,
            clock=application_clock,
            cycle_schedule=await conference_schedule_service.snapshot(owner_user_id),
        )

    @router.callback_query(F.data == "usage:refresh")
    async def refresh(
        callback: CallbackQuery,
        ollama_usage_service: OllamaUsageService,
        application_clock: ApplicationClock,
        conference_schedule_service: CycleScheduleService,
        owner_user_id: int,
    ) -> None:
        await refresh_usage(
            callback,
            ollama_usage_service,
            owner_user_id=owner_user_id,
            clock=application_clock,
            cycle_schedule=await conference_schedule_service.snapshot(owner_user_id),
        )

    @router.callback_query(F.data == "usage:model")
    async def choose_model(
        callback: CallbackQuery,
        state: FSMContext,
        model_registry: ModelRegistry,
    ) -> None:
        await choose_usage_model(callback, state, model_registry)

    return router


def format_usage_snapshot(
    snapshot: OllamaUsageSnapshot,
    *,
    clock: ApplicationClock | None = None,
    cycle_schedule: CycleScheduleView | None = None,
) -> str:
    active = "не выбрана"
    if snapshot.active_profile_id and snapshot.active_model_name:
        active = f"{escape(snapshot.active_profile_id)} / {escape(snapshot.active_model_name)}"
    lines = [
        f"Подключение: {active}",
    ]
    if clock is not None:
        lines.extend(
            [
                f"Временная зона: {escape(str(clock.timezone))}",
                f"Запущен: {clock.started_at:%Y-%m-%d %H:%M}",
                f"Текущее время: {clock.now():%Y-%m-%d %H:%M}",
                f"Поиск конференций: начиная с {clock.today():%Y-%m-%d}",
            ]
        )
    if cycle_schedule is not None:
        if cycle_schedule.last_run_id is None:
            lines.append("Последний цикл: ещё не запускался")
        else:
            status = RUN_STATUS_LABELS.get(
                cycle_schedule.last_status or "", cycle_schedule.last_status or "неизвестно"
            )
            last_finished_at = cycle_schedule.last_finished_at
            if last_finished_at is not None and clock is not None:
                last_finished_at = last_finished_at.astimezone(clock.timezone)
            finished = (
                f", {last_finished_at:%Y-%m-%d %H:%M}"
                if last_finished_at is not None
                else ""
            )
            lines.append(
                f"Последний цикл: №{cycle_schedule.last_run_id}, {status}{finished}"
            )
        lines.append(
            "Следующий цикл: понедельник, "
            f"{cycle_schedule.next_run_at:%Y-%m-%d %H:%M}"
        )
    lines.extend(["", "<b>OLLAMA FREE USAGE</b>", f"Статус: {STATUS_LABELS[snapshot.status]}"])
    if snapshot.status_observed_at is not None:
        observed = snapshot.status_observed_at.astimezone(UTC)
        lines.append(f"Последнее наблюдение: {observed:%Y-%m-%d %H:%M} UTC")

    if not snapshot.models:
        lines.extend(["", "Ollama Cloud не настроен."])
    else:
        for index, model in enumerate(snapshot.models, start=1):
            lines.extend(["", f"{index}. {escape(model.model_name)}"])
            if not model.observed:
                lines.append("Локальных наблюдений пока нет.")
                continue
            lines.append(
                "Сегодня: "
                f"запросы {model.today.request_count}; "
                f"вход {model.today.prompt_tokens}; "
                f"кеш {model.today.cached_prompt_tokens}; "
                f"выход {model.today.output_tokens}"
            )
            lines.append(
                "За календарный месяц (локально): "
                f"запросы {model.month.request_count}; "
                f"вход {model.month.prompt_tokens}; "
                f"кеш {model.month.cached_prompt_tokens}; "
                f"выход {model.month.output_tokens}"
            )

    lines.extend(
        [
            "",
            "Точный Free usage доступен только в Ollama Settings. "
            "Локальные токены не являются балансом аккаунта.",
        ]
    )
    return "\n".join(lines)


async def show_usage(
    message: Message,
    state,
    service: OllamaUsageService,
    *,
    owner_user_id: int,
    clock: ApplicationClock | None = None,
    cycle_schedule: CycleScheduleView | None = None,
) -> None:
    await state.clear()
    snapshot = await service.snapshot(owner_user_id, refresh_models=True)
    await _send_snapshot(
        message, snapshot, clock=clock, cycle_schedule=cycle_schedule
    )


async def refresh_usage(
    callback: CallbackQuery,
    service: OllamaUsageService,
    *,
    owner_user_id: int,
    clock: ApplicationClock | None = None,
    cycle_schedule: CycleScheduleView | None = None,
) -> None:
    snapshot = await service.snapshot(owner_user_id, refresh_models=True)
    await _send_snapshot(
        callback.message, snapshot, clock=clock, cycle_schedule=cycle_schedule
    )
    await callback.answer()


async def choose_usage_model(callback: CallbackQuery, state, registry: ModelRegistry) -> None:
    await state.clear()
    await list_model_profiles(callback.message, registry, state)
    await callback.answer()


async def _send_snapshot(
    target,
    snapshot: OllamaUsageSnapshot,
    *,
    clock: ApplicationClock | None = None,
    cycle_schedule: CycleScheduleView | None = None,
) -> None:
    await target.answer(
        format_usage_snapshot(snapshot, clock=clock, cycle_schedule=cycle_schedule),
        parse_mode="HTML",
        reply_markup=ollama_usage_keyboard(offer_model_choice=_offer_model_choice(snapshot)),
    )


def _offer_model_choice(snapshot: OllamaUsageSnapshot) -> bool:
    return snapshot.status in {
        ProviderUsageStatus.RATE_LIMITED,
        ProviderUsageStatus.FREE_EXHAUSTED,
    } and any(model_name != snapshot.active_model_name for model_name in snapshot.available_models)
