import asyncio
from collections import deque

import pytest

from conference_scout.agent.actions import ConfirmationService
from conference_scout.agent.memory import MemoryService
from conference_scout.agent.runtime import AgentRuntime
from conference_scout.ai.models import AvailableModel, ChatResponse, ToolCall
from conference_scout.ai.registry import ModelRegistry
from conference_scout.app import _build_action_executors
from conference_scout.db.models import Conference, ConferenceTopic, Topic
from conference_scout.db.repositories.agent_state import AgentStateRepository
from conference_scout.db.repositories.conferences import ConferenceRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.mcp.client import InProcessToolClient
from conference_scout.mcp.lifecycle import MCPServerLifecycle
from conference_scout.mcp.server import build_mcp_server
from conference_scout.mcp.tools import MCPToolServices
from conference_scout.schemas.agent import ArchiveConferenceAction
from conference_scout.schemas.common import ConferenceStatus


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

    async def chat(self, _model, _messages, _tools=()):
        return self.responses.popleft()


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
    registry = ModelRegistry.for_testing(state, local_gateway=gateway)
    await registry.select(user.id, "ollama-local", "local-model")
    await state.set_capability("ollama-local", "local-model", True)
    executors = _build_action_executors(session, user.id, None, None, None, None)
    confirmation = ConfirmationService(session, executors=executors)
    tool_client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=user.id,
                read_handlers={},
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
