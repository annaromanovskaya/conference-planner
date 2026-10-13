from datetime import UTC, datetime

import pytest

from conference_scout.mcp.client import InProcessToolClient
from conference_scout.mcp.server import build_mcp_server
from conference_scout.mcp.tools import PROPOSAL_TOOL_NAMES, READ_TOOL_NAMES, MCPToolServices
from conference_scout.schemas.agent import PendingActionView
from conference_scout.schemas.common import PendingActionStatus


class FakeConfirmation:
    def __init__(self) -> None:
        self.payloads = []

    async def propose(self, user_id, payload):
        self.payloads.append((user_id, payload))
        return PendingActionView(
            id=42,
            action_type=payload.action,
            summary=payload.confirmation_summary(),
            status=PendingActionStatus.PENDING,
            expires_at=datetime(2026, 10, 13, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_server_exposes_only_approved_closed_tools() -> None:
    services = MCPToolServices(owner_id=7, read_handlers={}, confirmation=FakeConfirmation())
    client = InProcessToolClient(build_mcp_server(services))

    tools = await client.list_tools()

    assert {tool.name for tool in tools} == READ_TOOL_NAMES | PROPOSAL_TOOL_NAMES
    assert len(tools) == 17
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
    forbidden = {"shell", "filesystem", "sql", "fetch_url", "web", "secret"}
    assert not ({tool.name for tool in tools} & forbidden)


@pytest.mark.asyncio
async def test_read_results_are_bounded_and_keep_references() -> None:
    async def search(owner_id: int, arguments: dict):
        assert owner_id == 7
        assert arguments == {"query": "astro", "limit": 2}
        return {
            "items": [
                {"kind": "conference", "id": 11, "name": "Astro One"},
                {"kind": "conference", "id": 12, "name": "Astro Two"},
                {"kind": "conference", "id": 13, "name": "Must be cut"},
            ],
            "provenance": {"source": "database"},
        }

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"search_knowledge": search},
        confirmation=FakeConfirmation(),
        max_items=2,
        max_result_chars=2_000,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("search_knowledge", {"query": "astro", "limit": 2})

    assert result.is_error is False
    assert result.structured_content["provenance"] == {"source": "database"}
    assert len(result.structured_content["items"]) == 2
    assert [(ref.kind, ref.id) for ref in result.references] == [
        ("conference", 11),
        ("conference", 12),
    ]


@pytest.mark.asyncio
async def test_proposal_returns_pending_id_without_executing_mutation() -> None:
    confirmation = FakeConfirmation()
    services = MCPToolServices(owner_id=7, read_handlers={}, confirmation=confirmation)
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("propose_track_conference", {"conference_id": 17})

    assert result.is_error is False
    assert result.structured_content["id"] == 42
    assert result.structured_content["status"] == "PENDING"
    assert len(confirmation.payloads) == 1
    assert confirmation.payloads[0][0] == 7
    assert confirmation.payloads[0][1].conference_id == 17


@pytest.mark.asyncio
async def test_unknown_reader_and_invalid_arguments_are_safe_errors() -> None:
    services = MCPToolServices(owner_id=7, read_handlers={}, confirmation=FakeConfirmation())
    client = InProcessToolClient(build_mcp_server(services))

    missing = await client.call_tool("list_topics", {"limit": 10})
    invalid = await client.call_tool("search_knowledge", {"query": "x", "limit": 10_000})
    unknown = await client.call_tool("run_shell", {"command": "whoami"})

    assert missing.is_error and "unavailable" in missing.content
    assert invalid.is_error
    assert unknown.is_error
