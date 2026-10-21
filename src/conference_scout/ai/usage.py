from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from conference_scout.ai.gateway import ModelGatewayError
from conference_scout.ai.registry import ModelRegistry
from conference_scout.db.repositories.ollama_usage import (
    OllamaUsageRepository,
    UsageAggregate,
)
from conference_scout.schemas.common import ProviderUsageStatus

EMPTY_USAGE = UsageAggregate(0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class ModelUsageView:
    model_name: str
    today: UsageAggregate
    month: UsageAggregate
    observed: bool


@dataclass(frozen=True, slots=True)
class OllamaUsageSnapshot:
    active_profile_id: str | None
    active_model_name: str | None
    models: tuple[ModelUsageView, ...]
    status: ProviderUsageStatus
    status_model_name: str | None
    status_observed_at: datetime | None
    detail_code: str | None
    available_models: tuple[str, ...]
    generated_at: datetime


class OllamaUsageService:
    def __init__(
        self,
        repository: OllamaUsageRepository,
        registry: ModelRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(self, user_id: int, *, refresh_models: bool) -> OllamaUsageSnapshot:
        now = self.clock().astimezone(UTC)
        try:
            configured_models = self.registry.allowed_models("ollama-cloud")
        except ModelGatewayError:
            configured_models = ()

        active_profile_id: str | None = None
        active_model_name: str | None = None
        try:
            active = await self.registry.get_active(user_id)
            active_profile_id = active.profile_id
            active_model_name = active.model_name
        except ModelGatewayError:
            pass

        daily_rows = await self.repository.get_daily_totals(user_id, "ollama-cloud", now.date())
        daily = {
            row.model_name: UsageAggregate(
                row.request_count,
                row.prompt_tokens,
                row.cached_prompt_tokens,
                row.output_tokens,
            )
            for row in daily_rows
        }
        month_start = date(now.year, now.month, 1)
        month_end = (
            date(now.year + 1, 1, 1) if now.month == 12 else date(now.year, now.month + 1, 1)
        )
        monthly = await self.repository.get_month_totals(
            user_id, "ollama-cloud", month_start, month_end
        )

        available_models: tuple[str, ...] = ()
        if refresh_models and configured_models:
            try:
                available_models = tuple(
                    model.name
                    for model in await self.registry.list_models("ollama-cloud", user_id=user_id)
                )
            except ModelGatewayError:
                available_models = ()

        state = await self.repository.get_status(user_id, "ollama-cloud")
        models = tuple(
            ModelUsageView(
                model_name=model_name,
                today=daily.get(model_name, EMPTY_USAGE),
                month=monthly.get(model_name, EMPTY_USAGE),
                observed=model_name in daily or model_name in monthly,
            )
            for model_name in configured_models
        )
        return OllamaUsageSnapshot(
            active_profile_id=active_profile_id,
            active_model_name=active_model_name,
            models=models,
            status=state.status if state is not None else ProviderUsageStatus.UNKNOWN,
            status_model_name=state.model_name if state is not None else None,
            status_observed_at=state.observed_at if state is not None else None,
            detail_code=state.detail_code if state is not None else None,
            available_models=available_models,
            generated_at=now,
        )
