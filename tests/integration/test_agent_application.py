import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from conference_scout.agent.actions import ConfirmationService
from conference_scout.agent.memory import MemoryService
from conference_scout.agent.runtime import AgentRuntime
from conference_scout.ai.models import (
    AvailableModel,
    ChatMessage,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)
from conference_scout.ai.registry import ModelRegistry, ProviderProfile
from conference_scout.app import (
    _build_action_executors,
    _find_accessible_conferences,
    _list_accessible_conferences,
    _MCPConfirmationProxy,
    _model_registry,
    _monitoring_failure_mapping,
)
from conference_scout.conferences.monitoring import MonitoringCheckError
from conference_scout.db.migrations import run_migrations
from conference_scout.db.models import (
    Conference,
    ConferenceTopic,
    Deadline,
    EventCandidate,
    Topic,
)
from conference_scout.db.repositories.agent_state import AgentStateRepository
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.db.repositories.ollama_usage import OllamaUsageRepository
from conference_scout.db.repositories.topics import TopicRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.db.session import create_engine_and_sessionmaker
from conference_scout.mcp.client import InProcessToolClient
from conference_scout.mcp.lifecycle import MCPServerLifecycle
from conference_scout.mcp.server import build_mcp_server
from conference_scout.mcp.tools import MCPToolServices
from conference_scout.schemas.agent import (
    AddTopicAction,
    ArchiveConferenceAction,
    SetCandidateDecisionAction,
    UpsertDeadlineAction,
)
from conference_scout.schemas.common import (
    CandidateDecisionReason,
    CandidateStatus,
    ConferenceStatus,
    DeadlineType,
    VerificationStatus,
)


class Gateway:
    def __init__(self):
        self.responses = deque(
            [
                ChatResponse(
                    tool_calls=(
                        ToolCall(
                            id="one",
                            name="propose_track_conference",
                            arguments={"conference_id": 1},
                        ),
                    )
                ),
                ChatResponse(content="Предлагаю отслеживать конференцию."),
            ]
        )

    async def list_models(self):
        return (AvailableModel(name="local-model"),)

    async def chat(
        self,
        _model,
        _messages,
        _tools=(),
        *,
        response_schema: dict[str, Any] | None = None,
    ):
        return self.responses.popleft()


class UsageGateway:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = deque(responses)

    async def list_models(self):
        return (AvailableModel(name="gemma4:31b"),)

    async def chat(
        self,
        _model: str,
        _messages: list[ChatMessage],
        _tools: list[ToolDefinition] = (),
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> ChatResponse:
        return self.responses.popleft()


def test_monitoring_failure_mapping_explains_safe_network_error() -> None:
    result = _monitoring_failure_mapping(
        2, MonitoringCheckError("NETWORK_ERROR")
    )

    assert result["committed"] is False
    assert result["conference_id"] == 2
    assert result["error_code"] == "NETWORK_ERROR"
    assert "открыть источники" in result["summary"].casefold()


@pytest.mark.asyncio
async def test_selected_model_to_mcp_proposal_to_confirmed_domain_action(session) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(100)
    conference = Conference(
        name="Astro 2027",
        normalized_name="astro 2027",
        website="https://astro.example",
        source_url="https://astro.example",
        status=ConferenceStatus.DISCOVERED,
    )
    session.add(conference)
    await session.flush()
    assert conference.id == 1
    topic = Topic(
        user_id=user.id,
        name="Astronomy",
        description="Stars",
        keywords=["astronomy"],
        enabled=True,
    )
    session.add(topic)
    await session.flush()
    session.add(
        ConferenceTopic(conference_id=conference.id, topic_id=topic.id, relevance_score=1.0)
    )

    gateway = Gateway()
    state = AgentStateRepository(session)
    registry = ModelRegistry.for_testing(state, llm_hub_gateway=gateway)
    await registry.select(user.id, "llm-hub", "local-model")
    await state.set_capability("llm-hub", "local-model", True)
    executors = _build_action_executors(session, user.id, None, None, None, None)
    confirmation = ConfirmationService(session, executors=executors)

    async def conference_context(owner_id: int, arguments: dict):
        assert owner_id == user.id
        return {
            "nodes": [
                {
                    "kind": "conference",
                    "id": arguments["conference_id"],
                    "label": "Astro 2027",
                }
            ]
        }

    tool_client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=user.id,
                read_handlers={"get_conference_context": conference_context},
                confirmation=confirmation,
            )
        )
    )
    runtime = AgentRuntime(
        registry,
        MemoryService(session, max_context_chars=4_000),
        tool_client,
        max_steps=3,
        max_tool_calls_per_step=3,
        timeout_seconds=5,
        max_tool_result_chars=2_000,
    )

    reply = await runtime.respond(user.id, "Отслеживай Astro 2027")
    result = await confirmation.confirm(user.id, reply.pending_actions[0].id)

    tracked = await ConferenceRepository(session).list_accessible_by_status(
        user.id, ConferenceStatus.TRACKING
    )
    assert result.success is True
    assert [item.id for item in tracked] == [conference.id]

    foreign = Conference(name="Foreign", normalized_name="foreign")
    session.add(foreign)
    await session.flush()
    with pytest.raises(ValueError, match="not found"):
        await executors["archive_conference"](
            user.id, ArchiveConferenceAction(conference_id=foreign.id)
        )


@pytest.mark.asyncio
async def test_mcp_lifecycle_stops_server_after_cancellation() -> None:
    lifecycle = MCPServerLifecycle(object(), host="127.0.0.1", port=8765)

    class Server:
        started = False

        def __init__(self):
            self._should_exit = False
            self.stopped = asyncio.Event()

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            self._should_exit = value
            if value:
                self.stopped.set()

        async def serve(self):
            self.started = True
            await self.stopped.wait()

    server = Server()
    lifecycle.server = server

    async with lifecycle:
        assert server.started is True

    assert server.should_exit is True


@pytest.mark.asyncio
async def test_agent_releases_sqlite_write_lock_before_separate_mcp_proposal(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agent-mcp.db'}?timeout=0.1"
    run_migrations(database_url)
    engine, session_factory = create_engine_and_sessionmaker(database_url)

    class TopicGateway:
        async def list_models(self):
            return (AvailableModel(name="tool-model"),)

        async def chat(
            self,
            _model,
            _messages,
            _tools=(),
            *,
            response_schema: dict[str, Any] | None = None,
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="topic-one",
                        name="propose_add_topic",
                        arguments={
                            "name": "Звёздная астрофизика",
                            "description": "Спектроскопия и ML",
                            "keywords": ["stellar spectroscopy", "astronomy machine learning"],
                        },
                    ),
                )
            )

    try:
        async with session_factory() as agent_session:
            user = await UserRepository(agent_session).get_or_create_by_telegram_id(777)
            registry = ModelRegistry.for_testing(
                AgentStateRepository(agent_session), llm_hub_gateway=TopicGateway()
            )
            await registry.select(user.id, "llm-hub", "tool-model")
            await registry.repository.set_capability("llm-hub", "tool-model", True)
            await agent_session.commit()
            tool_client = InProcessToolClient(
                build_mcp_server(
                    MCPToolServices(
                        owner_id=user.id,
                        read_handlers={},
                        confirmation=_MCPConfirmationProxy(session_factory, user.id),
                    )
                )
            )
            runtime = AgentRuntime(
                registry,
                MemoryService(agent_session, max_context_chars=4_000),
                tool_client,
                max_steps=2,
                max_tool_calls_per_step=2,
                timeout_seconds=2,
                max_tool_result_chars=2_000,
            )

            reply = await runtime.respond(user.id, "Добавь тему про звёздную астрофизику")

            assert reply.error_code is None
            assert len(reply.pending_actions) == 1
            assert reply.pending_actions[0].action_type == "add_topic"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_topic_executor_repairs_legacy_name_only_pending_action(session) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(778)
    executors = _build_action_executors(session, user.id, None, None, None, None)

    result = await executors["add_topic"](user.id, AddTopicAction(name="Звёздная астрофизика и ML"))

    topics = await TopicRepository(session).list_for_user(user.id)
    assert result == "Тема добавлена."
    assert topics[0].description == "Звёздная астрофизика и ML"
    assert topics[0].keywords == ["Звёздная астрофизика и ML"]


@pytest.mark.asyncio
async def test_candidate_decision_executor_is_confirmed_and_owner_scoped(session) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(7_780)
    foreign = await UserRepository(session).get_or_create_by_telegram_id(7_781)
    expires_at = datetime(2027, 1, 1, tzinfo=UTC)
    candidates = [
        EventCandidate(
            user_id=user_id,
            title=title,
            normalized_title=title.casefold(),
            source_url=url,
            query="stars",
            relevance_score=1.0,
            verification_status=VerificationStatus.UNVERIFIABLE,
            status=CandidateStatus.ACTIVE,
            expires_at=expires_at,
        )
        for user_id, title, url in (
            (owner.id, "Owner candidate", "https://owner.example/event"),
            (foreign.id, "Foreign candidate", "https://foreign.example/event"),
        )
    ]
    session.add_all(candidates)
    await session.flush()
    executors = _build_action_executors(session, owner.id, None, None, None, None)
    confirmation = ConfirmationService(session, executors=executors)
    payload = SetCandidateDecisionAction(
        candidate_id=candidates[0].id,
        decision="REJECTED",
    )

    pending = await confirmation.propose(owner.id, payload)
    assert candidates[0].status == CandidateStatus.ACTIVE

    result = await confirmation.confirm(owner.id, pending.id)

    assert result.success is True
    assert candidates[0].status == CandidateStatus.REJECTED
    assert candidates[0].decision_reason == CandidateDecisionReason.USER_REJECTED
    with pytest.raises(ValueError, match="candidate not found"):
        await executors["set_candidate_decision"](
            owner.id,
            SetCandidateDecisionAction(
                candidate_id=candidates[1].id,
                decision="REJECTED",
            ),
        )


@pytest.mark.asyncio
async def test_manual_approximate_deadline_is_written_only_after_confirmation(session) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(779)
    foreign_same_name = Conference(
        name="IMBASE 2027",
        normalized_name="imbase 2027",
        source_url="https://foreign.example/imbase",
    )
    conference = Conference(
        name="IMBASE 2027",
        normalized_name="imbase 2027",
        website="https://meetings.iac.es/imbase2027/",
        source_url="https://meetings.iac.es/imbase2027/",
    )
    session.add_all((foreign_same_name, conference))
    await session.flush()
    await ConferenceRepository(session).track(user.id, conference.id)
    confirmation = ConfirmationService(
        session,
        executors=_build_action_executors(
            session, user.id, None, None, None, None
        ),
    )
    payload = UpsertDeadlineAction(
        conference_id=conference.id,
        deadline_type=DeadlineType.REGISTRATION,
        approximate_year=2027,
        approximate_month=5,
    )

    pending = await confirmation.propose(user.id, payload)
    assert await session.scalar(select(func.count(Deadline.id))) == 0

    result = await confirmation.confirm(user.id, pending.id)
    stored = await session.scalar(select(Deadline))

    assert result.success is True
    assert "committed=true" in result.message
    assert stored is not None
    assert stored.conference_id == conference.id
    assert stored.conference_id != foreign_same_name.id
    assert stored.deadline_type == DeadlineType.REGISTRATION
    assert stored.deadline_date is None
    assert stored.approximate_year == 2027
    assert stored.approximate_month == 5
    assert stored.tentative is True

    retry = await confirmation.propose(user.id, payload)
    await confirmation.confirm(user.id, retry.id)
    assert await session.scalar(select(func.count(Deadline.id))) == 1


@pytest.mark.asyncio
async def test_ambiguous_conference_search_is_owner_scoped_and_read_only(session) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(780)
    foreign_user = await UserRepository(session).get_or_create_by_telegram_id(781)
    conferences = [
        Conference(name="IMBASE 2027", normalized_name="imbase 2027"),
        Conference(name="IMBASE Summer School", normalized_name="imbase summer school"),
        Conference(name="IMBASE Foreign", normalized_name="imbase foreign"),
    ]
    session.add_all(conferences)
    await session.flush()
    repository = ConferenceRepository(session)
    await repository.track(owner.id, conferences[0].id)
    await repository.track(owner.id, conferences[1].id)
    await repository.track(foreign_user.id, conferences[2].id)

    matches = await _find_accessible_conferences(
        session, owner.id, "IMBASE", limit=10
    )

    assert [item.id for item in matches] == [conferences[0].id, conferences[1].id]
    assert await session.scalar(select(func.count(Deadline.id))) == 0


@pytest.mark.asyncio
async def test_tracking_list_uses_owner_subscription_not_global_conference_status(session) -> None:
    owner = await UserRepository(session).get_or_create_by_telegram_id(782)
    conference = Conference(
        name="Tracked but discovered",
        normalized_name="tracked but discovered",
        status=ConferenceStatus.DISCOVERED,
    )
    session.add(conference)
    await session.flush()
    await ConferenceRepository(session).track(owner.id, conference.id)

    values = await _list_accessible_conferences(
        session,
        owner.id,
        status=ConferenceStatus.TRACKING,
        limit=20,
    )

    assert [item.id for item in values] == [conference.id]


@pytest.mark.asyncio
async def test_ollama_usage_is_wired_for_agent_reply_and_summary(session) -> None:
    class NoTools:
        async def list_tools(self):
            return ()

        async def call_tool(self, *_args):
            raise AssertionError("chat-only model must not call tools")

    user = await UserRepository(session).get_or_create_by_telegram_id(880)
    gateway = UsageGateway(
        [
            ChatResponse(content="Длинный основной ответ", prompt_tokens=10, output_tokens=4),
            ChatResponse(content="Резюме", prompt_tokens=6, output_tokens=2),
        ]
    )
    registry = _model_registry(
        session,
        (ProviderProfile("ollama-cloud", "Ollama Cloud", gateway, ("gemma4:31b",)),),
    )
    await registry.select(user.id, "ollama-cloud", "gemma4:31b")
    await registry.repository.set_capability("ollama-cloud", "gemma4:31b", False)
    runtime = AgentRuntime(
        registry,
        MemoryService(session, max_context_chars=30),
        NoTools(),
        max_steps=2,
        max_tool_calls_per_step=2,
        timeout_seconds=5,
        max_tool_result_chars=2_000,
    )

    reply = await runtime.respond(user.id, "Длинный пользовательский вопрос")
    rows = await OllamaUsageRepository(session).get_daily_totals(
        user.id, "ollama-cloud", datetime.now(UTC).date()
    )

    assert reply.text == "Длинный основной ответ"
    assert len(rows) == 1
    assert rows[0].request_count == 2
    assert rows[0].prompt_tokens == 16
    assert rows[0].output_tokens == 6


@pytest.mark.asyncio
async def test_llm_hub_usage_is_not_written_to_ollama_tables(session) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(881)
    gateway = UsageGateway([ChatResponse(content="ok", prompt_tokens=99, output_tokens=9)])
    registry = _model_registry(session, (ProviderProfile("llm-hub", "LLM Hub", gateway),))

    await registry.chat(
        user.id,
        await registry.select(user.id, "llm-hub", "gemma4:31b"),
        [ChatMessage(role="user", content="hello")],
    )

    assert (
        await OllamaUsageRepository(session).get_daily_totals(
            user.id, "ollama-cloud", datetime.now(UTC).date()
        )
        == ()
    )
