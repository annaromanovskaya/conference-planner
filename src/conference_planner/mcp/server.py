import json
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from conference_planner.mcp.tools import MCPToolServices
from conference_planner.schemas.agent import (
    AddSourceAction,
    AddTopicAction,
    ArchiveConferenceAction,
    DeleteConferenceAction,
    DeleteSourceAction,
    DeleteTopicAction,
    ForgetMemoryAction,
    ImportConferenceCandidateAction,
    ImportConferenceURLAction,
    RunDiscoveryAction,
    SaveMemoryAction,
    SetCandidateDecisionAction,
    TrackConferenceAction,
    UpsertDeadlineAction,
)
from conference_planner.schemas.common import DeadlineType, MemoryKind, MemoryScope, SourceType

Limit = Annotated[int, Field(ge=1, le=50)]
Days = Annotated[int, Field(ge=1, le=3_650)]
PositiveId = Annotated[int, Field(gt=0)]
Page = Annotated[int, Field(ge=1, le=10_000)]


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
    automatic_update = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
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
        description=(
            "List configured conference discovery sources and whether each source is enabled. "
            "Use this for questions about source URLs or source monitoring."
        ),
        annotations=read_only,
    )
    async def list_sources(limit: Limit = 20) -> Any:
        return await read("list_sources", {"limit": limit})

    @server.tool(
        description=(
            "List owner-accessible conferences. TRACKING and ARCHIVED filter by the "
            "owner subscription status, not by the shared conference row status."
        ),
        annotations=read_only,
    )
    async def list_conferences(status: str | None = None, limit: Limit = 20) -> Any:
        return await read("list_conferences", {"status": status, "limit": limit})

    @server.tool(
        description=(
            "Find owner-accessible persisted conferences by name before proposing a mutation. "
            "If several items are returned, ask the owner to choose one numeric ID."
        ),
        annotations=read_only,
    )
    async def find_conferences(query: str, limit: Limit = 10) -> Any:
        return await read("find_conferences", {"query": query, "limit": limit})

    @server.tool(
        description="Get the provenance-bearing knowledge context for one conference.",
        annotations=read_only,
    )
    async def get_conference_context(conference_id: PositiveId) -> Any:
        return await read("get_conference_context", {"conference_id": conference_id})

    @server.tool(
        description=(
            "List exact and approximate upcoming deadlines for owner-tracked conferences, "
            "relative to the application local date."
        ),
        annotations=read_only,
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
        description=(
            "Search one internet-results page for conference candidates. Increase page to "
            "continue beyond earlier results; use get_candidate for deep verification."
        ),
        annotations=read_only,
    )
    async def search_conference_candidates(query: str, limit: Limit = 20, page: Page = 1) -> Any:
        return await read(
            "search_conference_candidates", {"query": query, "limit": limit, "page": page}
        )

    @server.tool(
        description=(
            "List the owner's active conference candidates waiting for review. "
            "Use this when the owner asks which candidates are pending or awaiting review."
        ),
        annotations=read_only,
    )
    async def list_pending_candidates(limit: Limit = 20) -> Any:
        return await read("list_pending_candidates", {"limit": limit})

    @server.tool(
        description="Inspect one event candidate through the safe verification pipeline.",
        annotations=read_only,
    )
    async def get_candidate(candidate_id: PositiveId) -> Any:
        return await read("get_candidate", {"candidate_id": candidate_id})

    @server.tool(
        description="List persisted facts, deadlines, sources, and history for one conference.",
        annotations=read_only,
    )
    async def list_conference_details(conference_id: PositiveId) -> Any:
        return await read("list_conference_details", {"conference_id": conference_id})

    @server.tool(
        description="Check one tracked conference now and return a committed persistence receipt.",
        annotations=automatic_update,
    )
    async def check_tracked_conference(conference_id: PositiveId) -> Any:
        return await read("check_tracked_conference", {"conference_id": conference_id})

    @server.tool(
        description="Start a bounded durable conference research workflow.",
        annotations=automatic_update,
    )
    async def start_conference_research(request_text: str) -> Any:
        return await read("start_conference_research", {"request_text": request_text})

    @server.tool(
        description="Get one owner-scoped durable research run.", annotations=read_only
    )
    async def get_agent_run(run_id: PositiveId) -> Any:
        return await read("get_agent_run", {"run_id": run_id})

    @server.tool(
        description="List candidate artifacts produced by one owner-scoped research run.",
        annotations=read_only,
    )
    async def list_agent_run_candidates(run_id: PositiveId, limit: Limit = 20) -> Any:
        return await read(
            "list_agent_run_candidates", {"run_id": run_id, "limit": limit}
        )

    @server.tool(
        description="Cancel an owner-scoped research run.", annotations=automatic_update
    )
    async def cancel_agent_run(run_id: PositiveId) -> Any:
        return await read("cancel_agent_run", {"run_id": run_id})

    @server.tool(
        description=(
            "Create a pending confirmation for one validated candidate artifact. "
            "This never persists a conference directly."
        ),
        annotations=proposal,
    )
    async def propose_persist_agent_candidate(
        run_id: PositiveId, artifact_id: PositiveId
    ) -> dict[str, Any]:
        candidate = await services.read(
            "propose_persist_agent_candidate",
            {"run_id": run_id, "artifact_id": artifact_id},
        )
        if not isinstance(candidate, dict):
            raise ValueError("candidate artifact not found")
        if isinstance(candidate.get("candidate_id"), int):
            action = ImportConferenceCandidateAction(
                candidate_id=candidate["candidate_id"]
            )
        elif isinstance(candidate.get("title"), str) and isinstance(
            candidate.get("source_url"), str
        ):
            action = ImportConferenceURLAction(
                title=candidate["title"], source_url=candidate["source_url"]
            )
        else:
            raise ValueError("candidate artifact not found")
        return await propose(action)

    @server.tool(
        description="List safe progress events after a sequence number.",
        annotations=read_only,
    )
    async def list_agent_progress(
        run_id: PositiveId, after_sequence: int = 0, limit: Limit = 20
    ) -> Any:
        return await read(
            "list_agent_progress",
            {"run_id": run_id, "after_sequence": after_sequence, "limit": limit},
        )

    @server.tool(
        description=(
            "List the owner's hidden deferred or rejected conference candidates. "
            "Use only when the owner explicitly asks for one of these lists."
        ),
        annotations=read_only,
    )
    async def list_candidate_decisions(
        status: Literal["SKIPPED", "REJECTED"], limit: Limit = 20
    ) -> Any:
        return await read(
            "list_candidate_decisions", {"status": status, "limit": limit}
        )

    async def propose(payload) -> dict[str, Any]:
        pending = await services.confirmation.propose(services.owner_id, payload)
        return pending.model_dump(mode="json")

    @server.tool(
        description="Propose adding a conference topic for owner confirmation.",
        annotations=proposal,
    )
    async def propose_add_topic(name: str, description: str, keywords: list[str]) -> dict[str, Any]:
        return await propose(
            AddTopicAction(name=name, description=description, keywords=tuple(keywords))
        )

    @server.tool(
        description="Propose deleting one owner topic after confirmation.",
        annotations=proposal,
    )
    async def propose_delete_topic(topic_id: PositiveId) -> dict[str, Any]:
        topic = await services.internal_read("get_topic", {"topic_id": topic_id})
        if topic is None or not str(topic.get("name", "")).strip():
            raise ToolError("topic not found")
        return await propose(
            DeleteTopicAction(topic_id=topic_id, name=str(topic["name"]).strip())
        )

    @server.tool(
        description="Propose adding a trusted source for owner confirmation.", annotations=proposal
    )
    async def propose_add_source(source_type: SourceType, value: str) -> dict[str, Any]:
        return await propose(AddSourceAction(source_type=source_type, value=value))

    @server.tool(
        description="Propose deleting one configured source after owner confirmation.",
        annotations=proposal,
    )
    async def propose_delete_source(source_id: PositiveId) -> dict[str, Any]:
        source = await services.internal_read("get_source", {"source_id": source_id})
        if source is None or not str(source.get("name", "")).strip():
            raise ToolError("source not found")
        return await propose(
            DeleteSourceAction(source_id=source_id, name=str(source["name"]).strip())
        )

    @server.tool(
        description=(
            "Propose tracking a persisted conference for owner confirmation. "
            "Use only a numeric ID returned by list_conferences; never use a search rank "
            "or candidate ID."
        ),
        annotations=proposal,
    )
    async def propose_track_conference(conference_id: PositiveId) -> dict[str, Any]:
        context = await services.read("get_conference_context", {"conference_id": conference_id})
        if hasattr(context, "model_dump"):
            context = context.model_dump(mode="json")
        if not isinstance(context, dict):
            raise ValueError("conference not found")
        direct_match = context.get("kind") == "conference" and context.get("id") == conference_id
        graph_match = any(
            isinstance(node, dict)
            and node.get("kind") == "conference"
            and node.get("id") == conference_id
            for node in context.get("nodes", [])
        )
        if not direct_match and not graph_match:
            raise ValueError("conference not found")
        return await propose(TrackConferenceAction(conference_id=conference_id))

    @server.tool(
        description=(
            "Propose removing one conference from the owner's lists after confirmation. "
            "Shared conference data used by other owners is preserved."
        ),
        annotations=proposal,
    )
    async def propose_delete_conference(conference_id: PositiveId) -> dict[str, Any]:
        conference = await services.internal_read(
            "get_conference", {"conference_id": conference_id}
        )
        if conference is None or not str(conference.get("name", "")).strip():
            raise ToolError("conference not found")
        return await propose(
            DeleteConferenceAction(
                conference_id=conference_id,
                name=str(conference["name"]).strip(),
            )
        )

    @server.tool(
        description=(
            "Propose verifying, saving, and tracking a temporary candidate returned by "
            "search_conference_candidates. Use this instead of propose_track_conference for "
            "durable candidate IDs."
        ),
        annotations=proposal,
    )
    async def propose_add_candidate(candidate_id: PositiveId) -> dict[str, Any]:
        candidate = await services.read("get_candidate", {"candidate_id": candidate_id})
        if not isinstance(candidate, dict):
            raise ValueError("candidate not found")
        return await propose(
            ImportConferenceCandidateAction(
                candidate_id=candidate_id,
            )
        )

    @server.tool(
        description=(
            "Propose verifying, saving, and tracking a conference from an official "
            "HTTP(S) page supplied by the owner or obtained from inspected search data. "
            "Use this when no valid durable candidate ID is available."
        ),
        annotations=proposal,
    )
    async def propose_import_conference_url(title: str, source_url: str) -> dict[str, Any]:
        return await propose(ImportConferenceURLAction(title=title, source_url=source_url))

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

    @server.tool(
        description=(
            "Propose adding or updating one deadline for an owner-accessible persisted "
            "conference. Use find_conferences first; if several matches are returned, ask "
            "the owner to choose an ID. Approximate dates must use approximate_year/month "
            "and tentative=true. This action requires owner confirmation."
        ),
        annotations=proposal,
    )
    async def propose_upsert_deadline(
        conference_id: PositiveId,
        deadline_type: DeadlineType,
        exact_date: date | None = None,
        approximate_year: int | None = None,
        approximate_month: int | None = None,
        tentative: bool = True,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        context = await services.read(
            "get_conference_context", {"conference_id": conference_id}
        )
        if hasattr(context, "model_dump"):
            context = context.model_dump(mode="json")
        if not isinstance(context, dict):
            raise ValueError("conference not found")
        direct_match = context.get("kind") == "conference" and context.get("id") == conference_id
        graph_match = any(
            isinstance(node, dict)
            and node.get("kind") == "conference"
            and node.get("id") == conference_id
            for node in context.get("nodes", [])
        )
        if not direct_match and not graph_match:
            raise ValueError("conference not found")
        return await propose(
            UpsertDeadlineAction(
                conference_id=conference_id,
                deadline_type=deadline_type,
                exact_date=exact_date,
                approximate_year=approximate_year,
                approximate_month=approximate_month,
                tentative=tentative,
                source_url=source_url,
            )
        )

    @server.tool(
        description=(
            "Propose restoring, deferring, or rejecting an owner-scoped candidate. "
            "The change is applied only after owner confirmation."
        ),
        annotations=proposal,
    )
    async def propose_candidate_decision(
        candidate_id: PositiveId,
        decision: Literal["ACTIVE", "SKIPPED", "REJECTED"],
    ) -> dict[str, Any]:
        return await propose(
            SetCandidateDecisionAction(
                candidate_id=candidate_id,
                decision=decision,
            )
        )

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
