from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.ai.models import ChatResponse
from conference_scout.db.models import OllamaUsageDaily, ProviderUsageState
from conference_scout.schemas.common import ProviderUsageStatus

MAX_COUNTER = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    request_count: int
    prompt_tokens: int
    cached_prompt_tokens: int
    output_tokens: int


class OllamaUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_success(
        self,
        user_id: int,
        profile_id: str,
        model_name: str,
        response: ChatResponse,
        observed_at: datetime,
    ) -> None:
        counters = (
            response.prompt_tokens,
            response.cached_prompt_tokens,
            response.output_tokens,
        )
        if any(value is not None for value in counters):
            await self._upsert_daily(
                user_id,
                profile_id,
                model_name,
                observed_at.date(),
                response,
                observed_at,
            )
        await self.record_status(
            user_id,
            profile_id,
            ProviderUsageStatus.AVAILABLE,
            model_name=model_name,
            observed_at=observed_at,
        )

    async def record_status(
        self,
        user_id: int,
        profile_id: str,
        status: ProviderUsageStatus,
        *,
        model_name: str | None,
        observed_at: datetime,
        detail_code: str | None = None,
    ) -> None:
        statement = insert(ProviderUsageState).values(
            user_id=user_id,
            profile_id=profile_id,
            status=status,
            model_name=model_name,
            observed_at=observed_at,
            detail_code=detail_code,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "profile_id"],
            set_={
                "status": status,
                "model_name": model_name,
                "observed_at": observed_at,
                "detail_code": detail_code,
            },
        )
        async with self.session.begin_nested():
            await self.session.execute(statement)

    async def get_daily_totals(
        self, user_id: int, profile_id: str, usage_date: date
    ) -> tuple[OllamaUsageDaily, ...]:
        rows = await self.session.scalars(
            select(OllamaUsageDaily)
            .where(
                OllamaUsageDaily.user_id == user_id,
                OllamaUsageDaily.profile_id == profile_id,
                OllamaUsageDaily.usage_date == usage_date,
            )
            .order_by(OllamaUsageDaily.model_name)
        )
        return tuple(rows)

    async def get_month_totals(
        self,
        user_id: int,
        profile_id: str,
        month_start: date,
        month_end: date,
    ) -> dict[str, UsageAggregate]:
        rows = await self.session.execute(
            select(
                OllamaUsageDaily.model_name,
                func.sum(OllamaUsageDaily.request_count),
                func.sum(OllamaUsageDaily.prompt_tokens),
                func.sum(OllamaUsageDaily.cached_prompt_tokens),
                func.sum(OllamaUsageDaily.output_tokens),
            )
            .where(
                OllamaUsageDaily.user_id == user_id,
                OllamaUsageDaily.profile_id == profile_id,
                OllamaUsageDaily.usage_date >= month_start,
                OllamaUsageDaily.usage_date < month_end,
            )
            .group_by(OllamaUsageDaily.model_name)
        )
        return {
            model_name: UsageAggregate(
                request_count=int(request_count),
                prompt_tokens=int(prompt_tokens),
                cached_prompt_tokens=int(cached_prompt_tokens),
                output_tokens=int(output_tokens),
            )
            for (
                model_name,
                request_count,
                prompt_tokens,
                cached_prompt_tokens,
                output_tokens,
            ) in rows
        }

    async def get_status(self, user_id: int, profile_id: str) -> ProviderUsageState | None:
        return await self.session.scalar(
            select(ProviderUsageState).where(
                ProviderUsageState.user_id == user_id,
                ProviderUsageState.profile_id == profile_id,
            )
        )

    async def _upsert_daily(
        self,
        user_id: int,
        profile_id: str,
        model_name: str,
        usage_date: date,
        response: ChatResponse,
        observed_at: datetime,
    ) -> None:
        values = {
            "user_id": user_id,
            "profile_id": profile_id,
            "model_name": model_name,
            "usage_date": usage_date,
            "request_count": 1,
            "prompt_tokens": response.prompt_tokens or 0,
            "cached_prompt_tokens": response.cached_prompt_tokens or 0,
            "output_tokens": response.output_tokens or 0,
            "created_at": observed_at,
            "updated_at": observed_at,
        }
        statement = insert(OllamaUsageDaily).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "profile_id", "model_name", "usage_date"],
            set_={
                "request_count": func.min(OllamaUsageDaily.request_count + 1, MAX_COUNTER),
                "prompt_tokens": func.min(
                    OllamaUsageDaily.prompt_tokens + values["prompt_tokens"], MAX_COUNTER
                ),
                "cached_prompt_tokens": func.min(
                    OllamaUsageDaily.cached_prompt_tokens + values["cached_prompt_tokens"],
                    MAX_COUNTER,
                ),
                "output_tokens": func.min(
                    OllamaUsageDaily.output_tokens + values["output_tokens"], MAX_COUNTER
                ),
                "updated_at": observed_at,
            },
        )
        async with self.session.begin_nested():
            await self.session.execute(statement)
