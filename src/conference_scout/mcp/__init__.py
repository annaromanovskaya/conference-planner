"""Internal, owner-scoped Model Context Protocol boundary."""

from conference_scout.mcp.client import InProcessToolClient, StreamableHTTPToolClient, ToolResult
from conference_scout.mcp.server import build_mcp_app, build_mcp_server
from conference_scout.mcp.tools import MCPToolServices

__all__ = [
    "InProcessToolClient",
    "MCPToolServices",
    "StreamableHTTPToolClient",
    "ToolResult",
    "build_mcp_app",
    "build_mcp_server",
]
