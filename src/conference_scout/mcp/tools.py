from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from conference_scout.schemas.agent import ActionPayload, PendingActionView

READ_TOOL_NAMES = {
    "list_topics",
    "list_conferences",
    "get_conference_context",
    "list_upcoming_deadlines",
    "search_knowledge",
    "find_related_conferences",
    "list_memories",
    "get_active_model",
    "search_event_candidates",
    "inspect_event_candidate",
}

PROPOSAL_TOOL_NAMES = {
    "propose_add_topic",
    "propose_add_source",
    "propose_track_conference",
    "propose_archive_conference",
    "propose_save_memory",
    "propose_forget_memory",
    "propose_run_discovery",
}

ReadHandler = Callable[[int, dict[str, Any]], Awaitable[Any]]


class ConfirmationPort(Protocol):
    async def propose(self, user_id: int, payload: ActionPayload) -> PendingActionView: ...


@dataclass(frozen=True, slots=True)
class MCPToolServices:
    owner_id: int
    read_handlers: Mapping[str, ReadHandler]
    confirmation: ConfirmationPort
    max_items: int = 20
    max_result_chars: int = 12_000

    async def read(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in READ_TOOL_NAMES:
            raise ValueError("tool is not approved")
        handler = self.read_handlers.get(name)
        if handler is None:
            raise ToolError("tool is temporarily unavailable")
        try:
            return await handler(self.owner_id, arguments)
        except Exception as exc:
            raise ToolError("tool is temporarily unavailable") from exc
