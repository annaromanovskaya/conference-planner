import asyncio
from collections import deque

import pytest

from conference_scout.agent.memory import MemoryService
from conference_scout.agent.runtime import AgentRuntime
from conference_scout.ai.models import ChatResponse, ToolCall, ToolDefinition
from conference_scout.ai.registry import ActiveModel
from conference_scout.db.repositories.users import UserRepository
from conference_scout.mcp.client import ToolResult
from conference_scout.schemas.agent import DomainReference


class FakeGateway:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    async def chat(self, model, messages, tools=()):
        self.calls.append((model, tuple(messages), tuple(tools)))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response()
        return response


class FakeRegistry:
    def __init__(self, gateway, *, supports_tools=True):
        self.gateway = gateway
        self.active = ActiveModel("local", "test-model", supports_tools)

    async def get_active(self, _user_id):
        return self.active

    def gateway_for(self, _profile_id):
        return self.gateway


class FakeTools:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(content='{"items": []}')

    async def list_tools(self):
        return (
            ToolDefinition(
                name="search_knowledge",
                description="Search knowledge",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


async def _runtime(session, gateway, tools=None, *, supports_tools=True, **limits):
    user = await UserRepository(session).get_or_create_by_telegram_id(101)
    await session.flush()
    memory = MemoryService(session, max_context_chars=4_000)
    runtime = AgentRuntime(
        FakeRegistry(gateway, supports_tools=supports_tools),
        memory,
        tools or FakeTools(),
        max_steps=limits.get("max_steps", 6),
        max_tool_calls_per_step=limits.get("max_tool_calls_per_step", 3),
        timeout_seconds=limits.get("timeout_seconds", 2),
        max_tool_result_chars=limits.get("max_tool_result_chars", 2_000),
    )
    return user.id, memory, runtime


@pytest.mark.asyncio
async def test_plain_response_is_persisted(session) -> None:
    gateway = FakeGateway(ChatResponse(content="Здравствуйте!"))
    user_id, memory, runtime = await _runtime(session, gateway)

    reply = await runtime.respond(user_id, "Привет")
    context = await memory.get_context(user_id, "")

    assert reply.text == "Здравствуйте!"
    assert [(item.role, item.content) for item in context.messages] == [
        ("user", "Привет"),
        ("assistant", "Здравствуйте!"),
    ]


@pytest.mark.asyncio
async def test_single_tool_result_returns_references_and_pending_actions(session) -> None:
    pending = {
        "id": 42,
        "action_type": "track_conference",
        "summary": "Отслеживать конференцию 3",
        "status": "PENDING",
        "expires_at": "2026-10-13T12:00:00Z",
    }
    tools = FakeTools(
        ToolResult(
            content='{"id": 42}',
            structured_content=pending,
            references=(DomainReference(kind="conference", id=3, position=1),),
        )
    )
    gateway = FakeGateway(
        ChatResponse(
            tool_calls=(
                ToolCall(id="call-1", name="search_knowledge", arguments={"query": "astro"}),
            )
        ),
        ChatResponse(content="Нашёл конференцию."),
    )
    user_id, _, runtime = await _runtime(session, gateway, tools)

    reply = await runtime.respond(user_id, "Найди astro")

    assert tools.calls == [("search_knowledge", {"query": "astro"})]
    assert reply.text == "Нашёл конференцию."
    assert reply.references[0].id == 3
    assert reply.pending_actions[0].id == 42
    assert gateway.calls[1][1][-1].role == "tool"


@pytest.mark.asyncio
async def test_chat_only_model_gets_no_tools_and_an_explicit_note(session) -> None:
    gateway = FakeGateway(ChatResponse(content="Ответ без инструментов"))
    user_id, _, runtime = await _runtime(session, gateway, supports_tools=False)

    await runtime.respond(user_id, "Вопрос")

    assert gateway.calls[0][2] == ()
    assert "недоступны" in gateway.calls[0][1][0].content


@pytest.mark.asyncio
async def test_unknown_and_schema_invalid_tools_are_not_executed(session) -> None:
    tools = FakeTools()
    gateway = FakeGateway(
        ChatResponse(tool_calls=(ToolCall(name="run_shell", arguments={}),)),
        ChatResponse(tool_calls=(ToolCall(name="search_knowledge", arguments={"query": 123}),)),
        ChatResponse(content="Не удалось использовать инструменты."),
    )
    user_id, _, runtime = await _runtime(session, gateway, tools)

    reply = await runtime.respond(user_id, "Сделай")

    assert reply.text == "Не удалось использовать инструменты."
    assert tools.calls == []
    assert "unknown tool" in gateway.calls[1][1][-1].content
    assert "invalid arguments" in gateway.calls[2][1][-1].content


@pytest.mark.asyncio
async def test_per_step_call_limit_stops_before_execution(session) -> None:
    calls = tuple(
        ToolCall(name="search_knowledge", arguments={"query": str(index)}) for index in range(4)
    )
    gateway = FakeGateway(ChatResponse(tool_calls=calls))
    tools = FakeTools()
    user_id, _, runtime = await _runtime(session, gateway, tools, max_tool_calls_per_step=3)

    reply = await runtime.respond(user_id, "loop")

    assert reply.error_code == "tool_call_limit"
    assert tools.calls == []


@pytest.mark.asyncio
async def test_step_limit_and_oversized_result_are_controlled(session) -> None:
    response = ChatResponse(
        tool_calls=(ToolCall(name="search_knowledge", arguments={"query": "x"}),)
    )
    gateway = FakeGateway(response, response)
    tools = FakeTools(ToolResult(content="x" * 5_000))
    user_id, _, runtime = await _runtime(
        session, gateway, tools, max_steps=2, max_tool_result_chars=100
    )

    reply = await runtime.respond(user_id, "loop")

    assert reply.error_code == "step_limit"
    assert len(gateway.calls[1][1][-1].content) <= 100


@pytest.mark.asyncio
async def test_total_timeout_is_a_safe_russian_error(session) -> None:
    async def hang():
        await asyncio.sleep(1)

    gateway = FakeGateway(hang)
    user_id, _, runtime = await _runtime(session, gateway, timeout_seconds=0.01)

    reply = await runtime.respond(user_id, "Ждать")

    assert reply.error_code == "timeout"
    assert "время" in reply.text.casefold()


@pytest.mark.asyncio
async def test_provider_failure_does_not_expose_exception(session) -> None:
    gateway = FakeGateway(RuntimeError("secret upstream body"))
    user_id, _, runtime = await _runtime(session, gateway)

    reply = await runtime.respond(user_id, "Вопрос")

    assert reply.error_code == "provider_failure"
    assert "secret" not in reply.text
