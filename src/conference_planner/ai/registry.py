import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from conference_planner.ai.gateway import ModelGateway, ModelGatewayError
from conference_planner.ai.models import (
    AvailableModel,
    ChatMessage,
    ChatResponse,
    ToolDefinition,
)
from conference_planner.ai.ollama import OllamaGateway
from conference_planner.ai.openai_compatible import OpenAICompatibleGateway
from conference_planner.config import Settings
from conference_planner.db.repositories.agent_state import AgentStateRepository
from conference_planner.db.repositories.ollama_usage import OllamaUsageRepository
from conference_planner.schemas.common import ModelRole, ProviderUsageStatus

logger = logging.getLogger(__name__)


class ModelSelectionRequired(ModelGatewayError):
    def __init__(self) -> None:
        super().__init__("Select a model before using the agent", code="selection_required")


class UnknownProfile(ModelGatewayError):
    def __init__(self, profile_id: str) -> None:
        super().__init__(f"Unknown model profile: {profile_id}", code="unknown_profile")


class UnknownModel(ModelGatewayError):
    def __init__(self, model_name: str) -> None:
        super().__init__(f"Model is not currently available: {model_name}", code="unknown_model")


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    id: str
    display_name: str
    gateway: ModelGateway
    allowed_models: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ActiveModel:
    profile_id: str
    model_name: str
    supports_tools: bool | None


def build_provider_profiles(
    settings: Settings, client: httpx.AsyncClient
) -> tuple[ProviderProfile, ...]:
    profiles: list[ProviderProfile] = []
    if settings.ollama_cloud_api_key is not None:
        profiles.append(
            ProviderProfile(
                id="ollama-cloud",
                display_name="Ollama Cloud",
                gateway=OllamaGateway(
                    client,
                    str(settings.ollama_cloud_base_url),
                    settings.ollama_cloud_api_key.get_secret_value(),
                ),
                allowed_models=settings.ollama_cloud_free_models,
            )
        )
    if settings.llm_local_base_url is not None:
        profiles.append(
            ProviderProfile(
                id="llm-local",
                display_name="Local LLM API",
                gateway=OpenAICompatibleGateway(
                    client,
                    str(settings.llm_local_base_url),
                    _secret_value(settings.llm_local_api_key),
                ),
            )
        )
    return tuple(profiles)


class ModelRegistry:
    def __init__(
        self,
        repository: AgentStateRepository,
        profiles: Sequence[ProviderProfile],
        *,
        usage_repository: OllamaUsageRepository | None = None,
        capability_timeout: float = 20,
        role_models: Mapping[ModelRole, str] | None = None,
    ) -> None:
        self.repository = repository
        self.usage_repository = usage_repository
        self._profiles = {profile.id: profile for profile in profiles}
        self.capability_timeout = capability_timeout
        self.role_models = dict(role_models or {})

    @classmethod
    def for_testing(
        cls,
        repository: AgentStateRepository,
        *,
        cloud_gateway: ModelGateway | None = None,
        llm_local_gateway: ModelGateway | None = None,
        cloud_allowlist: tuple[str, ...] = (),
        usage_repository: OllamaUsageRepository | None = None,
        capability_timeout: float = 20,
        role_models: Mapping[ModelRole, str] | None = None,
    ) -> "ModelRegistry":
        profiles: list[ProviderProfile] = []
        if cloud_gateway is not None:
            profiles.append(
                ProviderProfile("ollama-cloud", "Ollama Cloud", cloud_gateway, cloud_allowlist)
            )
        if llm_local_gateway is not None:
            profiles.append(ProviderProfile("llm-local", "Local LLM API", llm_local_gateway))
        return cls(
            repository,
            profiles,
            usage_repository=usage_repository,
            capability_timeout=capability_timeout,
            role_models=role_models,
        )

    def available_profiles(self) -> tuple[ProviderProfile, ...]:
        return tuple(self._profiles.values())

    def gateway_for(self, profile_id: str) -> ModelGateway:
        return self._profile(profile_id).gateway

    def allowed_models(self, profile_id: str) -> tuple[str, ...]:
        return self._profile(profile_id).allowed_models or ()

    async def chat(
        self,
        user_id: int,
        active: ActiveModel,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> ChatResponse:
        gateway = self.gateway_for(active.profile_id)
        try:
            response = await gateway.chat(
                active.model_name,
                messages,
                tools,
                response_schema=response_schema,
            )
        except ModelGatewayError as exc:
            await self._record_failure(user_id, active, exc)
            raise
        if active.profile_id == "ollama-cloud" and self.usage_repository is not None:
            try:
                await self.usage_repository.record_success(
                    user_id,
                    active.profile_id,
                    active.model_name,
                    response,
                    datetime.now(UTC),
                )
            except Exception:
                logger.warning("Ollama usage recording failed", exc_info=True)
        return response

    async def list_models(
        self, profile_id: str, *, user_id: int | None = None
    ) -> tuple[AvailableModel, ...]:
        profile = self._profile(profile_id)
        try:
            models = tuple(await profile.gateway.list_models())
        except ModelGatewayError as exc:
            if user_id is not None:
                await self._record_profile_failure(user_id, profile_id, None, exc)
            raise
        if profile.allowed_models is None:
            return models
        by_name = {model.name: model for model in models}
        return tuple(by_name[name] for name in profile.allowed_models if name in by_name)

    async def select(self, user_id: int, profile_id: str, model_name: str) -> ActiveModel:
        models = await self.list_models(profile_id, user_id=user_id)
        if model_name not in {model.name for model in models}:
            raise UnknownModel(model_name)
        await self.repository.set_selection(user_id, profile_id, model_name)
        return ActiveModel(profile_id, model_name, None)

    async def get_active(self, user_id: int) -> ActiveModel:
        selection = await self.repository.get_selection(user_id)
        if selection is None:
            raise ModelSelectionRequired
        self._profile(selection.profile_id)
        capability = await self.repository.get_capability(
            selection.profile_id, selection.model_name
        )
        return ActiveModel(
            selection.profile_id,
            selection.model_name,
            capability.supports_tools if capability is not None else None,
        )

    async def get_for_role(self, user_id: int, role: ModelRole) -> ActiveModel:
        active = await self.get_active(user_id)
        override = self.role_models.get(role)
        if not override:
            return active
        profile_id, model_name = active.profile_id, override
        if "/" in override:
            profile_id, model_name = override.split("/", 1)
        models = await self.list_models(profile_id, user_id=user_id)
        if model_name not in {item.name for item in models}:
            raise UnknownModel(model_name)
        capability = await self.repository.get_capability(profile_id, model_name)
        return ActiveModel(
            profile_id,
            model_name,
            capability.supports_tools if capability is not None else None,
        )

    async def refresh_capability(self, user_id: int) -> ActiveModel:
        active = await self.get_active(user_id)
        probe = ToolDefinition(
            name="capability_probe",
            description="Return the supplied harmless value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        async with asyncio.timeout(self.capability_timeout):
            response = await self.chat(
                user_id,
                active,
                [
                    ChatMessage(role="system", content="Use the provided tool for this test."),
                    ChatMessage(role="user", content="Call capability_probe with value ok."),
                ],
                [probe],
            )
        supports_tools = any(call.name == probe.name for call in response.tool_calls)
        await self.repository.set_capability(active.profile_id, active.model_name, supports_tools)
        return ActiveModel(active.profile_id, active.model_name, supports_tools)

    async def _record_failure(
        self, user_id: int, active: ActiveModel, error: ModelGatewayError
    ) -> None:
        await self._record_profile_failure(user_id, active.profile_id, active.model_name, error)

    async def _record_profile_failure(
        self,
        user_id: int,
        profile_id: str,
        model_name: str | None,
        error: ModelGatewayError,
    ) -> None:
        if profile_id != "ollama-cloud" or self.usage_repository is None:
            return
        status = {
            "authentication": ProviderUsageStatus.AUTH_ERROR,
            "rate_limited": ProviderUsageStatus.RATE_LIMITED,
            "free_exhausted": ProviderUsageStatus.FREE_EXHAUSTED,
        }.get(error.code, ProviderUsageStatus.UNKNOWN)
        try:
            await self.usage_repository.record_status(
                user_id,
                profile_id,
                status,
                model_name=model_name,
                observed_at=datetime.now(UTC),
                detail_code=error.code,
            )
        except Exception:
            logger.warning("Ollama usage status recording failed", exc_info=True)

    def _profile(self, profile_id: str) -> ProviderProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise UnknownProfile(profile_id) from exc


def _secret_value(value) -> str | None:
    return value.get_secret_value() if value is not None else None
