import json
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from conference_scout.mcp.tools import MCPToolServices
from conference_scout.schemas.agent import (
    AddSourceAction,
    AddTopicAction,
    ArchiveConferenceAction,
    ForgetMemoryAction,
    RunDiscoveryAction,
    SaveMemoryAction,
    TrackConferenceAction,
)
from conference_scout.schemas.common import MemoryKind, MemoryScope, SourceType

Limit = Annotated[int, Field(ge=1, le=50)]
Days = Annotated[int, Field(ge=1, le=3_650)]
PositiveId = Annotated[int, Field(gt=0)]


def build_mcp_server(services: MCPToolServices) -> MCPServer:
    server = MCPServer(
        "ConfPlanner",
        instructions="Owner-scoped conference tools. Mutations only create pending actions.",
    )
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    proposal = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )

    async def read(name: str, arguments: dict[str, Any]) -> Any:
        return _bounded(
            await services.read(name, arguments),
            services.max_items,
            services.max_result_chars,
        )

    @server.tool(
        description="List the owner's configured conference topics.", annotations=read_only
    )
    async def list_topics(limit: Limit = 20) -> Any:
        return await read("list_topics", {"limit": limit})

    @server.tool(
        description="List accessible conferences, optionally filtered by status.",
        annotations=read_only,
    )
    async def list_conferences(status: str | None = None, limit: Limit = 20) -> Any:
        return await read("list_conferences", {"status": status, "limit": limit})

    @server.tool(
        description="Get the provenance-bearing knowledge context for one conference.",
        annotations=read_only,
    )
    async def get_conference_context(conference_id: PositiveId) -> Any:
        return await read("get_conference_context", {"conference_id": conference_id})

    @server.tool(
        description="List upcoming deadlines for accessible conferences.", annotations=read_only
    )
    async def list_upcoming_deadlines(days: Days = 365, limit: Limit = 20) -> Any:
        return await read("list_upcoming_deadlines", {"days": days, "limit": limit})

    @server.tool(
        description="Search the owner's conference knowledge graph.", annotations=read_only
    )
    async def search_knowledge(query: str, limit: Limit = 10) -> Any:
        return await read("search_knowledge", {"query": query, "limit": limit})

    @server.tool(
        description="Find owner-accessible conferences related to a conference.",
        annotations=read_only,
    )
    async def find_related_conferences(conference_id: PositiveId, limit: Limit = 10) -> Any:
        return await read(
            "find_related_conferences", {"conference_id": conference_id, "limit": limit}
        )

    @server.tool(description="List sanitized active long-term memories.", annotations=read_only)
    async def list_memories(limit: Limit = 20) -> Any:
        return await read("list_memories", {"limit": limit})

    @server.tool(
        description="Get the selected provider and model capabilities.", annotations=read_only
    )
    async def get_active_model() -> Any:
        return await read("get_active_model", {})

    @server.tool(
        description="Search event candidates through the bounded conference research pipeline.",
        annotations=read_only,
    )
    async def search_event_candidates(query: str, limit: Limit = 10) -> Any:
        return await read("search_event_candidates", {"query": query, "limit": limit})

    @server.tool(
        description="Inspect one event candidate through the safe verification pipeline.",
        annotations=read_only,
    )
    async def inspect_event_candidate(candidate_id: str) -> Any:
        return await read("inspect_event_candidate", {"candidate_id": candidate_id})

    async def propose(payload) -> dict[str, Any]:
        pending = await services.confirmation.propose(services.owner_id, payload)
        return pending.model_dump(mode="json")

    @server.tool(
        description="Propose adding a conference topic for owner confirmation.",
        annotations=proposal,
    )
    async def propose_add_topic(
        name: str, description: str = "", keywords: list[str] | None = None
    ) -> dict[str, Any]:
        return await propose(
            AddTopicAction(name=name, description=description, keywords=tuple(keywords or ()))
        )

    @server.tool(
        description="Propose adding a trusted source for owner confirmation.", annotations=proposal
    )
    async def propose_add_source(source_type: SourceType, value: str) -> dict[str, Any]:
        return await propose(AddSourceAction(source_type=source_type, value=value))

    @server.tool(
        description="Propose tracking a conference for owner confirmation.", annotations=proposal
    )
    async def propose_track_conference(conference_id: PositiveId) -> dict[str, Any]:
        return await propose(TrackConferenceAction(conference_id=conference_id))

    @server.tool(
        description="Propose archiving a conference for owner confirmation.", annotations=proposal
    )
    async def propose_archive_conference(conference_id: PositiveId) -> dict[str, Any]:
        return await propose(ArchiveConferenceAction(conference_id=conference_id))

    @server.tool(
        description="Propose saving a user memory for owner confirmation.", annotations=proposal
    )
    async def propose_save_memory(
        kind: MemoryKind,
        scope: MemoryScope,
        original_text: str,
        rule_data: dict[str, Any],
        topic_id: int | None = None,
        conference_id: int | None = None,
    ) -> dict[str, Any]:
        return await propose(
            SaveMemoryAction(
                kind=kind,
                scope=scope,
                original_text=original_text,
                rule_data=rule_data,
                provenance={"source": "agent"},
                topic_id=topic_id,
                conference_id=conference_id,
            )
        )

    @server.tool(
        description="Propose forgetting a user memory for owner confirmation.", annotations=proposal
    )
    async def propose_forget_memory(memory_id: PositiveId) -> dict[str, Any]:
        return await propose(ForgetMemoryAction(memory_id=memory_id))

    @server.tool(
        description="Propose running conference discovery for owner confirmation.",
        annotations=proposal,
    )
    async def propose_run_discovery(years: list[int]) -> dict[str, Any]:
        return await propose(RunDiscoveryAction(years=tuple(years)))

    _close_input_schemas(server)
    return server


def build_mcp_app(server: MCPServer, *, host: str, port: int):
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("MCP transport must bind to loopback")
    security = TransportSecuritySettings(
        allowed_hosts=[host, f"{host}:{port}"],
        allowed_origins=[],
    )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host=host,
    )


def _close_input_schemas(server: MCPServer) -> None:
    # MCP SDK v2 derives schemas from signatures but does not mark the root closed.
    # Closing it is part of this application's model-facing security contract.
    for tool in server._tool_manager._tools.values():
        tool.parameters["additionalProperties"] = False


def _bounded(value: Any, max_items: int, max_chars: int) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        value = {str(key): _bounded(item, max_items, max_chars) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_bounded(item, max_items, max_chars) for item in value[:max_items]]
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        return {"truncated": True, "preview": encoded[:max_chars]}
    return value
