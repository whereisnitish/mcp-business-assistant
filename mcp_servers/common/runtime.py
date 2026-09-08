"""Shared runtime for every MCP server in this project.

Each server is a real, independently runnable MCP server built on the official
Python SDK (``mcp.server.mcpserver.MCPServer``). Nothing about the protocol is
re-implemented here -- this module only supplies the things all five servers need:

* a consistent ``MCPServer`` instance with logging that never touches stdout,
* one database session per tool call,
* translation of business exceptions into MCP's *anticipated failure* channel,
* a CLI entry point supporting both stdio and Streamable HTTP transports.

**Why stdout is off limits:** under the stdio transport the server speaks JSON-RPC
over stdout. A stray ``print()`` corrupts the protocol stream and the client's next
parse fails. All logging goes to stderr.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger, safe_extra
from app.db.session import session_scope
from app.integrations.base import IntegrationError

logger = get_logger(__name__)

T = TypeVar("T")


def build_server(name: str, *, instructions: str, version: str = "1.0.0") -> MCPServer:
    """Create a configured :class:`MCPServer`.

    ``instructions`` is served to clients during initialisation and is the server's
    own description of what it is for -- the agent reads it alongside the tool list.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return MCPServer(name=name, instructions=instructions, version=version)


@asynccontextmanager
async def tool_session() -> AsyncIterator[AsyncSession]:
    """One transactional database session per tool call.

    A tool call is the natural unit of work: it either commits as a whole or leaves
    nothing behind. Holding a session open across calls would leak transaction state
    between unrelated agent steps.
    """
    async with session_scope() as session:
        yield session


def tool_error(message: str) -> ToolError:
    """Build an *anticipated* tool failure.

    The SDK forwards a :class:`ToolError` message to the client (and therefore to
    the model) and logs it without a traceback, while any other exception is
    reported as a bare ``Error executing tool <name>`` with the detail kept on the
    server. So this is used exclusively for failures that are safe to disclose and
    actionable by the agent -- "lead not found", "end date before start date". A
    genuine bug must stay masked.
    """
    return ToolError(message)


async def run_tool(operation: str, func: Callable[[], Any]) -> Any:
    """Execute a tool body, translating known failures and timing the call.

    ``AppError`` and ``IntegrationError`` carry client-safe messages by contract, so
    they become ``ToolError``. Anything else propagates untouched and the SDK masks
    it, which is the behaviour we want for an unexpected bug.
    """
    try:
        result = await func()
    except IntegrationError as exc:
        logger.info(
            "tool reported an integration failure",
            extra=safe_extra({"event": "mcp.tool_integration_error", "operation": operation}),
        )
        raise tool_error(exc.message) from exc
    except AppError as exc:
        logger.info(
            "tool reported a business failure",
            extra=safe_extra({"event": "mcp.tool_business_error", "operation": operation}),
        )
        raise tool_error(exc.message) from exc
    except ValueError as exc:
        # Argument-shaped problems the tool itself detected (bad enum, bad range).
        raise tool_error(str(exc)) from exc
    return result


# --------------------------------------------------------------------------- #
# Argument coercion helpers
# --------------------------------------------------------------------------- #
def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime.

    Language models routinely emit timestamps without an offset. Comparing a naive
    value against an aware one raises, so every boundary normalises here rather
    than each tool remembering to.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return the UTC ``[start, end)`` bounds of a calendar day."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, datetime.combine(day, time.max, tzinfo=UTC)


def resolve_period(
    start_date: date | None, end_date: date | None, *, default_days: int
) -> tuple[date, date]:
    """Resolve an optional date range into a concrete one.

    Falls back to the last ``default_days`` ending today, and silently swaps a
    reversed range instead of failing -- a model that emits start/end backwards
    should still get the report it obviously meant.
    """
    from datetime import timedelta

    today = datetime.now(UTC).date()
    end = end_date or today
    start = start_date or (end - timedelta(days=default_days - 1))
    return (end, start) if start > end else (start, end)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_server_cli(server: MCPServer, *, default_port: int = 9000) -> None:
    """Parse CLI arguments and run *server* on the requested transport.

    ``stdio`` is the default: the API process launches each server as a subprocess
    and speaks to it over the pipe. ``http`` runs the same server as a standalone
    Streamable HTTP service, which is how tool servers are deployed when different
    teams own them -- the tools and their behaviour are identical either way.
    """
    parser = argparse.ArgumentParser(description=f"Run the {server.name} MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("MCP_SERVER_TRANSPORT", "stdio"),
        help="stdio (default, subprocess) or http (standalone Streamable HTTP service)",
    )
    parser.add_argument("--host", default=os.getenv("MCP_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_SERVER_PORT", default_port)))
    args = parser.parse_args()

    if args.transport == "http":
        logger.info(
            "starting MCP server over Streamable HTTP",
            extra=safe_extra(
                {
                    "event": "mcp.server_start",
                    "server": server.name,
                    "host": args.host,
                    "port": args.port,
                }
            ),
        )
        # `run()` forwards **kwargs to run_streamable_http_async(host=, port=, ...).
        server.run(transport="streamable-http", host=args.host, port=args.port)
        return

    logger.info(
        "starting MCP server over stdio",
        extra=safe_extra({"event": "mcp.server_start", "server": server.name}),
    )
    server.run(transport="stdio")
