"""MCP client layer: connections, dynamic tool discovery and call routing.

Note the package name shadows nothing: absolute imports mean ``from mcp import
Client`` inside this package still resolves to the installed SDK, not to itself.
"""

from app.mcp.client import ConnectionState, MCPServerConnection
from app.mcp.config import MCPServerSpec, build_server_specs
from app.mcp.discovery import ToolCatalog, build_tool_spec
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall, ToolResult, ToolSpec, qualify, split_qualified

__all__ = [
    "ConnectionState",
    "MCPManager",
    "MCPServerConnection",
    "MCPServerSpec",
    "ToolCall",
    "ToolCatalog",
    "ToolResult",
    "ToolSpec",
    "build_server_specs",
    "build_tool_spec",
    "qualify",
    "split_qualified",
]
