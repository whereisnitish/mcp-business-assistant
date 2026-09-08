"""MCP connection manager.

Owns the lifetime of every server connection and routes tool calls to the right
one. Two behaviours are worth calling out:

**Degraded mode.** Servers connect concurrently, and by default a failure to reach
one does not prevent startup: the API comes up with a smaller catalog and reports
the failure through ``/health``. An assistant that cannot send email is far more
useful than an assistant that will not start. ``MCP_FAIL_FAST=true`` inverts this
for deployments that would rather refuse than serve a partial capability set.

**Routing is derived from the tool name.** ``crm__get_leads`` goes to the ``crm``
connection because the name says so, not because of a hand-maintained map. A tool
that is not in the catalog is refused before any transport is touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import MCPServerUnavailableError, ToolNotFoundError
from app.core.logging import get_logger, safe_extra
from app.mcp.client import MCPServerConnection
from app.mcp.config import MCPServerSpec, build_server_specs, unknown_servers
from app.mcp.discovery import ToolCatalog, build_tool_spec
from app.mcp.types import ToolCall, ToolResult, ToolSpec

logger = get_logger(__name__)


class MCPManager:
    """Lifecycle and routing for a set of MCP server connections."""

    def __init__(
        self, specs: list[MCPServerSpec] | None = None, settings: Settings | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._specs = specs if specs is not None else build_server_specs(self._settings)
        self._connections: dict[str, MCPServerConnection] = {}
        self._catalog = ToolCatalog()
        self._failures: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle --
    async def startup(self) -> None:
        """Connect to every configured server and build the tool catalog."""
        for name in unknown_servers(self._settings):
            logger.warning(
                "ignoring unknown MCP server name in configuration",
                extra=safe_extra({"event": "mcp.unknown_server", "server": name}),
            )

        results = await asyncio.gather(
            *(self._connect_one(spec) for spec in self._specs), return_exceptions=True
        )
        connected = sum(1 for outcome in results if outcome is True)

        if self._failures and self._settings.mcp_fail_fast:
            await self.shutdown()
            failed = ", ".join(sorted(self._failures))
            raise MCPServerUnavailableError(failed, "MCP_FAIL_FAST is enabled")

        await self.refresh_catalog()
        logger.info(
            "MCP manager started",
            extra=safe_extra(
                {
                    "event": "mcp.manager_started",
                    "connected_servers": connected,
                    "failed_servers": sorted(self._failures),
                    "tool_count": len(self._catalog),
                }
            ),
        )

    async def _connect_one(self, spec: MCPServerSpec) -> bool:
        connection = MCPServerConnection(spec, call_timeout=self._settings.mcp_call_timeout_seconds)
        self._connections[spec.name] = connection
        try:
            await connection.connect(timeout=self._settings.mcp_startup_timeout_seconds)
        except Exception as exc:
            self._failures[spec.name] = connection.error or str(exc)
            logger.warning(
                "MCP server unavailable; continuing without it",
                extra=safe_extra(
                    {
                        "event": "mcp.server_unavailable",
                        "server": spec.name,
                        "reason": self._failures[spec.name],
                    }
                ),
            )
            return False
        self._failures.pop(spec.name, None)
        return True

    async def shutdown(self) -> None:
        """Disconnect every server. Never raises -- this runs during shutdown."""
        await asyncio.gather(
            *(connection.disconnect() for connection in self._connections.values()),
            return_exceptions=True,
        )
        self._connections.clear()
        self._catalog = ToolCatalog()
        logger.info("MCP manager stopped", extra=safe_extra({"event": "mcp.manager_stopped"}))

    # ---------------------------------------------------------------- discovery --
    async def refresh_catalog(self) -> ToolCatalog:
        """Re-run discovery against every connected server.

        A server that fails mid-discovery is recorded and skipped; the tools of the
        healthy servers still make it into the catalog.
        """
        async with self._lock:
            specs: list[ToolSpec] = []
            for name, connection in self._connections.items():
                if not connection.is_connected:
                    continue
                try:
                    tools: list[Any] = await connection.list_tools()
                except Exception as exc:
                    self._failures[name] = f"tool discovery failed: {type(exc).__name__}"
                    logger.warning(
                        "MCP tool discovery failed",
                        extra=safe_extra(
                            {
                                "event": "mcp.discovery_failed",
                                "server": name,
                                "error_type": type(exc).__name__,
                            }
                        ),
                    )
                    continue
                specs.extend(build_tool_spec(name, tool) for tool in tools)

            self._catalog = ToolCatalog(specs)
            logger.info(
                "MCP tool discovery completed",
                extra=safe_extra(
                    {
                        "event": "mcp.tools_discovered",
                        "tool_count": len(self._catalog),
                        "servers": self._catalog.servers,
                        **self._catalog.stats(),
                    }
                ),
            )
            return self._catalog

    @property
    def catalog(self) -> ToolCatalog:
        """The most recent discovery snapshot."""
        return self._catalog

    # ------------------------------------------------------------------- calls --
    async def call_tool(self, call: ToolCall, *, timeout: float | None = None) -> ToolResult:
        """Route a call to the owning server.

        This method performs **no permission checking**. Authorisation happens in
        :class:`~app.services.tool_execution_service.ToolExecutionService`, which is
        the only component that should ever call this one. Keeping the split sharp
        means there is exactly one place to audit the security decision.
        """
        spec = self._catalog.get(call.tool_name)
        if spec is None:
            raise ToolNotFoundError(call.tool_name)

        connection = self._connections.get(spec.server)
        if connection is None or not connection.is_connected:
            raise MCPServerUnavailableError(spec.server, self._failures.get(spec.server))

        return await connection.call_tool(call, timeout=timeout)

    # ------------------------------------------------------------------ health --
    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failures)

    @property
    def is_degraded(self) -> bool:
        """True when at least one configured server is unavailable."""
        return bool(self._failures)

    def connection_states(self) -> dict[str, str]:
        return {name: connection.state.value for name, connection in self._connections.items()}

    async def health(self) -> dict[str, Any]:
        """Live health snapshot, pinging each connection."""
        pings = await asyncio.gather(
            *(connection.ping() for connection in self._connections.values()),
            return_exceptions=True,
        )
        servers = {
            name: {
                "state": connection.state.value,
                "transport": connection.spec.transport,
                "reachable": ping is True,
                "tool_count": len(self._catalog.by_server(name)),
                **({"error": self._failures[name]} if name in self._failures else {}),
            }
            for (name, connection), ping in zip(self._connections.items(), pings, strict=False)
        }
        return {
            "degraded": self.is_degraded,
            "tool_count": len(self._catalog),
            "tools_by_permission": self._catalog.stats(),
            "servers": servers,
        }
