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
    assert len(tools) == 31
    assert all(tool.description for tool in tools)
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
    add_topic = next(tool for tool in tools if tool.name == "propose_add_topic")
    assert set(add_topic.input_schema["required"]) == {"name", "description", "keywords"}
    search = next(tool for tool in tools if tool.name == "search_conference_candidates")
    assert "page" in search.input_schema["properties"]
    assert "find_conferences" in {tool.name for tool in tools}
    assert "propose_upsert_deadline" in {tool.name for tool in tools}
    assert "list_candidate_decisions" in {tool.name for tool in tools}
    assert "propose_candidate_decision" in {tool.name for tool in tools}
    forbidden = {"shell", "filesystem", "sql", "fetch_url", "web", "secret"}
    assert not ({tool.name for tool in tools} & forbidden)


@pytest.mark.asyncio
async def test_workflow_tools_are_owner_scoped_and_persistence_stays_pending() -> None:
    confirmation = FakeConfirmation()
    calls = []

    async def handler(owner_id, arguments):
        calls.append((owner_id, arguments))
        return {"candidate_id": 81}

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"propose_persist_agent_candidate": handler},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool(
        "propose_persist_agent_candidate", {"run_id": 5, "artifact_id": 9}
    )

    assert result.is_error is False
    assert calls == [(7, {"run_id": 5, "artifact_id": 9})]
    assert confirmation.payloads[0][1].candidate_id == 81
    assert result.structured_content["status"] == "PENDING"


@pytest.mark.asyncio
async def test_workflow_url_artifact_creates_url_import_confirmation() -> None:
    confirmation = FakeConfirmation()

    async def handler(_owner_id, _arguments):
        return {
            "title": "Direct conference",
            "source_url": "https://events.example.org/direct",
        }

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"propose_persist_agent_candidate": handler},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool(
        "propose_persist_agent_candidate", {"run_id": 5, "artifact_id": 9}
    )

    assert result.is_error is False
    payload = confirmation.payloads[0][1]
    assert payload.action == "import_conference_url"
    assert payload.title == "Direct conference"
    assert str(payload.source_url) == "https://events.example.org/direct"


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

    async def context(_owner_id, arguments):
        return {"kind": "conference", "id": arguments["conference_id"]}

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"get_conference_context": context},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("propose_track_conference", {"conference_id": 17})

    assert result.is_error is False
    assert result.structured_content["id"] == 42
    assert result.structured_content["status"] == "PENDING"
    assert len(confirmation.payloads) == 1
    assert confirmation.payloads[0][0] == 7
    assert confirmation.payloads[0][1].conference_id == 17


@pytest.mark.asyncio
async def test_find_conferences_is_owner_scoped_and_deadline_proposal_stays_pending() -> None:
    confirmation = FakeConfirmation()
    calls = []

    async def find(owner_id, arguments):
        calls.append((owner_id, arguments))
        return {
            "items": [
                {"kind": "conference", "id": 17, "name": "IMBASE 2027"},
                {"kind": "conference", "id": 18, "name": "IMBASE Workshop"},
            ]
        }

    async def context(owner_id, arguments):
        calls.append((owner_id, arguments))
        return {"kind": "conference", "id": arguments["conference_id"]}

    client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=7,
                read_handlers={
                    "find_conferences": find,
                    "get_conference_context": context,
                },
                confirmation=confirmation,
            )
        )
    )

    found = await client.call_tool("find_conferences", {"query": "IMBASE", "limit": 10})
    proposed = await client.call_tool(
        "propose_upsert_deadline",
        {
            "conference_id": 17,
            "deadline_type": "REGISTRATION",
            "approximate_year": 2027,
            "approximate_month": 5,
            "tentative": True,
        },
    )

    assert found.is_error is False
    assert [item["id"] for item in found.structured_content["items"]] == [17, 18]
    assert all(call[0] == 7 for call in calls)
    assert proposed.structured_content["status"] == "PENDING"
    assert confirmation.payloads[0][1].action == "upsert_deadline"
    assert confirmation.payloads[0][1].approximate_month == 5


@pytest.mark.asyncio
async def test_list_conferences_forwards_tracking_subscription_filter() -> None:
    calls = []

    async def list_conferences(owner_id, arguments):
        calls.append((owner_id, arguments))
        return {
            "items": [
                {
                    "kind": "conference",
                    "id": 17,
                    "name": "IMBASE 2027",
                    "status": "TRACKING",
                }
            ]
        }

    client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=7,
                read_handlers={"list_conferences": list_conferences},
                confirmation=FakeConfirmation(),
            )
        )
    )

    result = await client.call_tool(
        "list_conferences", {"status": "TRACKING", "limit": 20}
    )

    assert result.is_error is False
    assert calls == [(7, {"status": "TRACKING", "limit": 20})]
    assert result.structured_content["items"][0]["status"] == "TRACKING"


@pytest.mark.asyncio
async def test_import_candidate_proposal_snapshots_server_candidate_data() -> None:
    confirmation = FakeConfirmation()

    async def inspect(_owner_id, arguments):
        assert arguments == {"candidate_id": 8}
        return {
            "id": 8,
            "title": "Verified Astro Meeting",
            "url": "https://astro.example/meeting",
            "page_text": "Meeting details",
        }

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"get_candidate": inspect},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("propose_add_candidate", {"candidate_id": 8})

    assert result.is_error is False
    payload = confirmation.payloads[0][1]
    assert payload.candidate_id == 8


@pytest.mark.asyncio
async def test_check_tracked_conference_returns_bounded_committed_receipt() -> None:
    async def check(owner_id, arguments):
        assert owner_id == 7
        assert arguments == {"conference_id": 11}
        return {
            "kind": "persistence_receipt",
            "committed": True,
            "conference_id": 11,
            "summary": "Обновлено: deadline:REGISTRATION.",
        }

    client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=7,
                read_handlers={"check_tracked_conference": check},
                confirmation=FakeConfirmation(),
            )
        )
    )

    result = await client.call_tool("check_tracked_conference", {"conference_id": 11})

    assert result.is_error is False
    assert result.structured_content["committed"] is True


@pytest.mark.asyncio
async def test_import_url_proposal_does_not_require_temporary_candidate() -> None:
    confirmation = FakeConfirmation()
    services = MCPToolServices(
        owner_id=7,
        read_handlers={},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool(
        "propose_import_conference_url",
        {
            "title": "IMBASE 2027",
            "source_url": "https://meetings.iac.es/imbase2027/",
        },
    )

    assert result.is_error is False
    payload = confirmation.payloads[0][1]
    assert payload.action == "import_conference_url"
    assert payload.title == "IMBASE 2027"
    assert str(payload.source_url) == "https://meetings.iac.es/imbase2027/"


@pytest.mark.asyncio
async def test_tracking_proposal_rejects_id_not_present_in_database() -> None:
    confirmation = FakeConfirmation()

    async def missing(_owner_id, _arguments):
        raise ValueError("conference not found")

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"get_conference_context": missing},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("propose_track_conference", {"conference_id": 11})

    assert result.is_error is True
    assert confirmation.payloads == []


@pytest.mark.asyncio
async def test_tracking_proposal_rejects_empty_knowledge_context() -> None:
    confirmation = FakeConfirmation()

    async def empty(_owner_id, _arguments):
        return {"nodes": [], "edges": []}

    services = MCPToolServices(
        owner_id=7,
        read_handlers={"get_conference_context": empty},
        confirmation=confirmation,
    )
    client = InProcessToolClient(build_mcp_server(services))

    result = await client.call_tool("propose_track_conference", {"conference_id": 11})

    assert result.is_error is True
    assert confirmation.payloads == []


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


@pytest.mark.asyncio
async def test_candidate_decision_list_is_owner_scoped_and_bounded() -> None:
    calls = []

    async def decisions(owner_id, arguments):
        calls.append((owner_id, arguments))
        return {
            "items": [
                {"kind": "event_candidate", "id": 1, "title": "One"},
                {"kind": "event_candidate", "id": 2, "title": "Two"},
                {"kind": "event_candidate", "id": 3, "title": "Three"},
            ]
        }

    client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=7,
                read_handlers={"list_candidate_decisions": decisions},
                confirmation=FakeConfirmation(),
                max_items=2,
            )
        )
    )

    result = await client.call_tool(
        "list_candidate_decisions", {"status": "REJECTED", "limit": 2}
    )

    assert result.is_error is False
    assert calls == [(7, {"status": "REJECTED", "limit": 2})]
    assert [item["id"] for item in result.structured_content["items"]] == [1, 2]


@pytest.mark.asyncio
async def test_candidate_decision_proposal_requires_confirmation_and_known_status() -> None:
    confirmation = FakeConfirmation()
    client = InProcessToolClient(
        build_mcp_server(
            MCPToolServices(
                owner_id=7,
                read_handlers={},
                confirmation=confirmation,
            )
        )
    )

    result = await client.call_tool(
        "propose_candidate_decision", {"candidate_id": 17, "decision": "REJECTED"}
    )
    invalid = await client.call_tool(
        "propose_candidate_decision", {"candidate_id": 17, "decision": "IMPORTED"}
    )

    assert result.is_error is False
    assert result.structured_content["status"] == "PENDING"
    assert len(confirmation.payloads) == 1
    assert confirmation.payloads[0][0] == 7
    assert confirmation.payloads[0][1].candidate_id == 17
    assert confirmation.payloads[0][1].decision == "REJECTED"
    assert invalid.is_error is True
