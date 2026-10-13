from collections.abc import Sequence

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.ai.gateway import ModelGateway
from conference_scout.ai.models import (
    AvailableModel,
    ChatMessage,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)
from conference_scout.ai.registry import (
    ModelRegistry,
    ModelSelectionRequired,
    UnknownModel,
    build_provider_profiles,
)
from conference_scout.config import Settings
from conference_scout.db.repositories.agent_state import AgentStateRepository
from conference_scout.db.repositories.users import UserRepository


class FakeGateway(ModelGateway):
    def __init__(self, names: Sequence[str], *, probe_response: ChatResponse | None = None) -> None:
        self.names = names
        self.probe_response = probe_response or ChatResponse(content="chat only")
        self.chat_models: list[str] = []

    async def list_models(self) -> Sequence[AvailableModel]:
        return tuple(AvailableModel(name=name) for name in self.names)

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> ChatResponse:
        self.chat_models.append(model)
        return self.probe_response


async def _owner_id(session: AsyncSession) -> int:
    owner = await UserRepository(session).get_or_create_by_telegram_id(123)
    await session.flush()
    return owner.id


@pytest.mark.asyncio
async def test_build_profiles_includes_only_configured_services() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="token",
        owner_telegram_id=123,
        ollama_cloud_api_key=SecretStr("cloud-key"),
        ollama_local_base_url="http://localhost:11434",
    )
    async with httpx.AsyncClient() as client:
        profiles = build_provider_profiles(settings, client)

    assert [profile.id for profile in profiles] == ["ollama-cloud", "ollama-local"]
    assert profiles[0].allowed_models == settings.ollama_cloud_free_models
    assert profiles[1].allowed_models is None


@pytest.mark.asyncio
async def test_cloud_models_are_filtered_in_allowlist_order(session: AsyncSession) -> None:
    gateway = FakeGateway(["other", "gemma4:31b", "gpt-oss:20b"])
    registry = ModelRegistry.for_testing(
        AgentStateRepository(session),
        cloud_gateway=gateway,
        cloud_allowlist=("gpt-oss:20b", "missing", "gemma4:31b"),
    )

    models = await registry.list_models("ollama-cloud")

    assert [model.name for model in models] == ["gpt-oss:20b", "gemma4:31b"]


@pytest.mark.asyncio
async def test_local_models_are_not_filtered(session: AsyncSession) -> None:
    registry = ModelRegistry.for_testing(
        AgentStateRepository(session), local_gateway=FakeGateway(["qwen3", "private-model"])
    )

    models = await registry.list_models("ollama-local")

    assert [model.name for model in models] == ["qwen3", "private-model"]


@pytest.mark.asyncio
async def test_selection_rejects_stale_model_and_persists_valid_choice(
    session: AsyncSession,
) -> None:
    owner_id = await _owner_id(session)
    registry = ModelRegistry.for_testing(
        AgentStateRepository(session), local_gateway=FakeGateway(["qwen3"])
    )

    with pytest.raises(UnknownModel):
        await registry.select(owner_id, "ollama-local", "removed-model")

    selected = await registry.select(owner_id, "ollama-local", "qwen3")
    await session.commit()
    active = await registry.get_active(owner_id)
    assert selected.profile_id == "ollama-local"
    assert active.profile_id == "ollama-local"
    assert active.model_name == "qwen3"


@pytest.mark.asyncio
async def test_missing_selection_has_explicit_error(session: AsyncSession) -> None:
    owner_id = await _owner_id(session)
    registry = ModelRegistry.for_testing(
        AgentStateRepository(session), local_gateway=FakeGateway(["qwen3"])
    )

    with pytest.raises(ModelSelectionRequired) as captured:
        await registry.get_active(owner_id)

    assert captured.value.code == "selection_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            ChatResponse(
                tool_calls=(ToolCall(name="capability_probe", arguments={"value": "ok"}),)
            ),
            True,
        ),
        (ChatResponse(content="I cannot use tools"), False),
    ],
)
async def test_capability_probe_is_persisted(
    session: AsyncSession, response: ChatResponse, expected: bool
) -> None:
    owner_id = await _owner_id(session)
    repository = AgentStateRepository(session)
    gateway = FakeGateway(["qwen3"], probe_response=response)
    registry = ModelRegistry.for_testing(repository, local_gateway=gateway, capability_timeout=1)
    await registry.select(owner_id, "ollama-local", "qwen3")

    active = await registry.refresh_capability(owner_id)
    stored = await repository.get_capability("ollama-local", "qwen3")

    assert active.supports_tools is expected
    assert stored is not None and stored.supports_tools is expected
