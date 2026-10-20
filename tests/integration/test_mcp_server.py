from datetime import UTC, datetime

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conference_scout.mcp.server import build_mcp_app, build_mcp_server
from conference_scout.mcp.tools import MCPToolServices
from conference_scout.schemas.agent import PendingActionView
from conference_scout.schemas.common import PendingActionStatus


class ConfirmationStub:
    async def propose(self, user_id, payload):
        assert user_id == 77
        return PendingActionView(
            id=9,
            action_type=payload.action,
            summary=payload.confirmation_summary(),
            status=PendingActionStatus.PENDING,
            expires_at=datetime(2026, 10, 13, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_streamable_http_negotiates_lists_and_calls_tools() -> None:
    async def topics(owner_id: int, arguments: dict):
        assert (owner_id, arguments) == (77, {"limit": 2})
        return {"items": [{"kind": "topic", "id": 3, "name": "Astronomy"}]}

    async def conference_context(owner_id: int, arguments: dict):
        assert owner_id == 77
        return {"kind": "conference", "id": arguments["conference_id"]}

    server = build_mcp_server(
        MCPToolServices(
            owner_id=77,
            read_handlers={
                "list_topics": topics,
                "get_conference_context": conference_context,
            },
            confirmation=ConfirmationStub(),
        )
    )
    app = build_mcp_app(server, host="127.0.0.1", port=8765)
    transport = httpx2.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as http_client:
            async with streamable_http_client(
                "http://127.0.0.1:8765/mcp", http_client=http_client
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    read = await session.call_tool("list_topics", {"limit": 2})
                    proposal = await session.call_tool(
                        "propose_track_conference", {"conference_id": 31}
                    )
                    invalid = await session.call_tool("list_topics", {"limit": 1_000})
                    unknown = await session.call_tool("run_shell", {})

    assert initialized.server_info.name == "ConfPlanner"
    assert len(tools.tools) == 31
    assert read.is_error is False and "Astronomy" in read.content[0].text
    assert proposal.is_error is False and '"id":9' in proposal.content[0].text.replace(" ", "")
    assert invalid.is_error is True
    assert unknown.is_error is True


def test_mcp_app_rejects_non_loopback_binding() -> None:
    server = build_mcp_server(
        MCPToolServices(owner_id=77, read_handlers={}, confirmation=ConfirmationStub())
    )

    with pytest.raises(ValueError, match="loopback"):
        build_mcp_app(server, host="0.0.0.0", port=8765)
