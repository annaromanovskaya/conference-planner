import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from conference_scout.ai.gateway import ModelGateway, ModelGatewayError
from conference_scout.ai.models import (
    AvailableModel,
    ChatMessage,
    ToolDefinition,
)
from conference_scout.ai.ollama import OllamaGateway
from conference_scout.ai.openai_compatible import OpenAICompatibleGateway
from conference_scout.config import Settings
from conference_scout.db.repositories.agent_state import AgentStateRepository


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
    if settings.ollama_local_base_url is not None:
        profiles.append(
            ProviderProfile(
                id="ollama-local",
                display_name="Локальный Ollama",
                gateway=OllamaGateway(
                    client,
                    str(settings.ollama_local_base_url),
                    _secret_value(settings.ollama_local_api_key),
                ),
            )
        )
    if settings.local_openai_base_url is not None:
        profiles.append(
            ProviderProfile(
                id="openai-local",
                display_name="Локальный OpenAI-compatible API",
                gateway=OpenAICompatibleGateway(
                    client,
                    str(settings.local_openai_base_url),
                    _secret_value(settings.local_openai_api_key),
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
        capability_timeout: float = 20,
    ) -> None:
        self.repository = repository
        self._profiles = {profile.id: profile for profile in profiles}
        self.capability_timeout = capability_timeout

    @classmethod
    def for_testing(
        cls,
        repository: AgentStateRepository,
        *,
        cloud_gateway: ModelGateway | None = None,
        local_gateway: ModelGateway | None = None,
        openai_gateway: ModelGateway | None = None,
        cloud_allowlist: tuple[str, ...] = (),
        capability_timeout: float = 20,
    ) -> "ModelRegistry":
        profiles: list[ProviderProfile] = []
        if cloud_gateway is not None:
            profiles.append(
                ProviderProfile("ollama-cloud", "Ollama Cloud", cloud_gateway, cloud_allowlist)
            )
        if local_gateway is not None:
            profiles.append(ProviderProfile("ollama-local", "Локальный Ollama", local_gateway))
        if openai_gateway is not None:
            profiles.append(ProviderProfile("openai-local", "Локальный OpenAI API", openai_gateway))
        return cls(repository, profiles, capability_timeout=capability_timeout)

    def available_profiles(self) -> tuple[ProviderProfile, ...]:
        return tuple(self._profiles.values())

    def gateway_for(self, profile_id: str) -> ModelGateway:
        return self._profile(profile_id).gateway

    async def list_models(self, profile_id: str) -> tuple[AvailableModel, ...]:
        profile = self._profile(profile_id)
        models = tuple(await profile.gateway.list_models())
        if profile.allowed_models is None:
            return models
        by_name = {model.name: model for model in models}
        return tuple(by_name[name] for name in profile.allowed_models if name in by_name)

    async def select(self, user_id: int, profile_id: str, model_name: str) -> ActiveModel:
        models = await self.list_models(profile_id)
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

    async def refresh_capability(self, user_id: int) -> ActiveModel:
        active = await self.get_active(user_id)
        gateway = self.gateway_for(active.profile_id)
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
            response = await gateway.chat(
                active.model_name,
                [
                    ChatMessage(role="system", content="Use the provided tool for this test."),
                    ChatMessage(role="user", content="Call capability_probe with value ok."),
                ],
                [probe],
            )
        supports_tools = any(call.name == probe.name for call in response.tool_calls)
        await self.repository.set_capability(active.profile_id, active.model_name, supports_tools)
        return ActiveModel(active.profile_id, active.model_name, supports_tools)

    def _profile(self, profile_id: str) -> ProviderProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise UnknownProfile(profile_id) from exc


def _secret_value(value) -> str | None:
    return value.get_secret_value() if value is not None else None
