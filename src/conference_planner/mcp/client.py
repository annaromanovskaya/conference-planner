import json
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from conference_planner.ai.models import ToolDefinition
from conference_planner.schemas.agent import DomainReference


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    structured_content: dict[str, Any] = Field(default_factory=dict)
    references: tuple[DomainReference, ...] = ()
    is_error: bool = False


class ToolClient(Protocol):
    async def list_tools(self) -> Sequence[ToolDefinition]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class InProcessToolClient:
    def __init__(self, server: MCPServer, *, max_result_chars: int = 12_000) -> None:
        self.server = server
        self.max_result_chars = max_result_chars

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        tools = await self.server.list_tools()
        return tuple(
            ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in tools
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self.server.call_tool(name, arguments)
            return _normalize_result(result, self.max_result_chars)
        except Exception as exc:
            return ToolResult(content=_safe_error(exc), is_error=True)


class StreamableHTTPToolClient:
    def __init__(self, url: str, *, timeout_seconds: float, max_result_chars: int) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_result_chars = max_result_chars
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "StreamableHTTPToolClient":
        stack = AsyncExitStack()
        streams = await stack.enter_async_context(streamable_http_client(self.url))
        session = await stack.enter_async_context(
            ClientSession(streams[0], streams[1], read_timeout_seconds=self.timeout_seconds)
        )
        await session.initialize()
        self._stack = stack
        self._session = session
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        result = await self._require_session().list_tools()
        return tuple(
            ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in result.tools
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self._require_session().call_tool(
                name, arguments, read_timeout_seconds=self.timeout_seconds
            )
            return _normalize_result(result, self.max_result_chars)
        except Exception as exc:
            return ToolResult(content=_safe_error(exc), is_error=True)

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP client is not connected")
        return self._session


def _normalize_result(result: Any, max_chars: int) -> ToolResult:
    structured = getattr(result, "structured_content", None) or {}
    content_parts = [getattr(item, "text", "") for item in getattr(result, "content", ())]
    content = "\n".join(part for part in content_parts if part)
    if not structured and content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
    encoded = json.dumps(structured, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        structured = {"truncated": True, "preview": encoded[:max_chars]}
        content = encoded[:max_chars]
    elif not content:
        content = encoded
    return ToolResult(
        content=content[:max_chars],
        structured_content=structured,
        references=_references(structured),
        is_error=bool(getattr(result, "is_error", False)),
    )


def _references(value: Any) -> tuple[DomainReference, ...]:
    found: list[DomainReference] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            kind, identifier = item.get("kind"), item.get("id")
            if isinstance(kind, str) and isinstance(identifier, int):
                found.append(DomainReference(kind=kind, id=identifier, position=len(found) + 1))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (ToolError, ValueError, TypeError, RuntimeError)):
        return str(exc)[:300]
    return "tool call failed"
