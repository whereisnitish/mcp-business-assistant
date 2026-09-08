"""A long-lived connection to a single MCP server.

The MCP SDK exposes a connection as an async context manager. That is a natural fit
for a script, but not for a web service where the connection must outlive any one
request and be used concurrently by many. Naively storing the entered context and
exiting it later fails: the SDK opens an ``anyio`` task group internally, and a task
group must be exited by the task that entered it. Doing otherwise raises
``Attempted to exit cancel scope in a different task``.

So each connection owns a **supervisor task** that enters the context, publishes the
live client, and then parks on a shutdown event. Request handlers borrow the client
and issue calls from their own tasks -- which is safe, because the underlying session
multiplexes requests over ``anyio`` streams -- while entry and exit both happen on
the supervisor task.

    supervisor task:   async with Client(...) as c:   c published ---> parked until stop
    request tasks:                                    c.call_tool(...) concurrently
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

from mcp import Client
from mcp.types import CallToolResult, TextContent

from app.core.exceptions import MCPServerUnavailableError, ToolExecutionError, ToolTimeoutError
from app.core.logging import Timer, get_logger, safe_extra
from app.mcp.config import MCPServerSpec
from app.mcp.types import ToolCall, ToolResult

logger = get_logger(__name__)


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


class MCPServerConnection:
    """Owns one server's transport, session lifetime and tool invocations."""

    def __init__(self, spec: MCPServerSpec, *, call_timeout: float = 30.0) -> None:
        self.spec = spec
        self.state = ConnectionState.DISCONNECTED
        self.error: str | None = None
        self._call_timeout = call_timeout
        self._client: Client | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED and self._client is not None

    # ---------------------------------------------------------------- lifecycle --
    async def connect(self, *, timeout: float = 30.0) -> None:
        """Start the supervisor task and wait until the session is initialised.

        Raises:
            MCPServerUnavailableError: the server could not be reached, failed to
                initialise, or did not become ready within *timeout*.
        """
        if self.is_connected:
            return

        self.state = ConnectionState.CONNECTING
        self.error = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._supervisor = asyncio.create_task(self._run(), name=f"mcp-connection-{self.name}")

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError as exc:
            await self._abort()
            self.state = ConnectionState.FAILED
            self.error = f"did not become ready within {timeout:g}s"
            raise MCPServerUnavailableError(self.name, self.error) from exc

        if self.state != ConnectionState.CONNECTED:
            # The supervisor set _ready to report a failure rather than a success.
            await self._abort()
            raise MCPServerUnavailableError(self.name, self.error or "connection failed")

        logger.info(
            "MCP server connected",
            extra=safe_extra(
                {"event": "mcp.connected", "server": self.name, "transport": self.spec.transport}
            ),
        )

    async def _run(self) -> None:
        """Supervisor body: hold the session open until asked to stop."""
        try:
            async with Client(self.spec.target()) as client:
                self._client = client
                self.state = ConnectionState.CONNECTED
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # BaseException, not Exception: the SDK surfaces transport failures
            # inside an ExceptionGroup, which does not inherit from Exception on
            # every path. A missed failure here would hang connect() until timeout.
            self.state = ConnectionState.FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "MCP server connection failed",
                extra=safe_extra(
                    {"event": "mcp.connect_failed", "server": self.name, "reason": self.error}
                ),
            )
        finally:
            self._client = None
            self._ready.set()
            if self.state == ConnectionState.CONNECTED:
                self.state = ConnectionState.CLOSED

    async def disconnect(self) -> None:
        """Signal the supervisor to exit and wait for the transport to close."""
        if self._supervisor is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._supervisor), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            await self._abort()
        finally:
            self._supervisor = None
            self._client = None
            self.state = ConnectionState.CLOSED
            logger.info(
                "MCP server disconnected",
                extra=safe_extra({"event": "mcp.disconnected", "server": self.name}),
            )

    async def _abort(self) -> None:
        """Cancel the supervisor task outright.

        Used when a connection failed or would not shut down cleanly. Awaiting the
        cancelled task is what guarantees the child process is reaped rather than
        left behind; whatever it raises on the way out is expected here and only
        logged at debug level, because this path runs *because* something already
        went wrong and a second error adds no information.
        """
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception) as exc:
                logger.debug(
                    "MCP connection aborted",
                    extra=safe_extra(
                        {
                            "event": "mcp.aborted",
                            "server": self.name,
                            "error_type": type(exc).__name__,
                        }
                    ),
                )
        self._supervisor = None
        self._client = None

    # ------------------------------------------------------------------- calls --
    async def list_tools(self) -> list[Any]:
        """Fetch the server's tool list. Returns SDK ``Tool`` objects."""
        client = self._require_client()
        result = await client.list_tools()
        return list(result.tools)

    async def call_tool(self, call: ToolCall, *, timeout: float | None = None) -> ToolResult:
        """Invoke a tool and normalise the outcome into a :class:`ToolResult`.

        Failures become values rather than exceptions wherever the agent could
        reasonably react to them (the tool reported an error, arguments were
        rejected). Transport-level problems still raise, because they mean the
        server is gone and retrying the same call would be pointless.
        """
        client = self._require_client()
        _, tool_name = call.tool_name.split("__", 1)
        timer = Timer()

        try:
            result: CallToolResult = await asyncio.wait_for(
                client.call_tool(tool_name, call.arguments), timeout=timeout or self._call_timeout
            )
        except TimeoutError as exc:
            raise ToolTimeoutError(call.tool_name, timeout or self._call_timeout) from exc
        except Exception as exc:
            # A protocol-level failure: the session is suspect from here on.
            logger.warning(
                "MCP tool call failed at the transport level",
                extra=safe_extra(
                    {
                        "event": "mcp.call_transport_error",
                        "server": self.name,
                        "tool": call.tool_name,
                        "error_type": type(exc).__name__,
                    }
                ),
            )
            raise ToolExecutionError(
                call.tool_name, f"transport error ({type(exc).__name__})"
            ) from exc

        return self._to_tool_result(call, result, timer.elapsed_ms)

    @staticmethod
    def _to_tool_result(call: ToolCall, result: CallToolResult, duration_ms: float) -> ToolResult:
        """Convert an SDK ``CallToolResult`` into the application's own type."""
        text = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent) and block.text
        ).strip()

        if result.is_error:
            return ToolResult.failure(
                call, text or "The tool reported an error.", duration_ms=duration_ms
            )

        structured = result.structured_content
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            # The SDK wraps a non-object return value in {"result": ...}. Unwrap only
            # when the payload is itself an object, so a list result keeps its shape.
            inner = structured["result"]
            if isinstance(inner, dict):
                structured = inner

        return ToolResult.ok(
            call,
            content=text or "(no textual content)",
            structured_content=structured,
            duration_ms=duration_ms,
        )

    def _require_client(self) -> Client:
        if self._client is None or not self.is_connected:
            raise MCPServerUnavailableError(self.name, self.error or "not connected")
        return self._client

    async def ping(self) -> bool:
        """Liveness probe used by the health endpoint.

        Implemented as a ``tools/list`` round trip rather than the protocol ping:
        ``send_ping`` was removed in the 2026-07-28 revision and now only functions
        in legacy mode, so it would report every healthy server as unreachable.
        Listing tools is slightly heavier but proves the session genuinely works.
        """
        if not self.is_connected:
            return False
        try:
            await asyncio.wait_for(self._require_client().list_tools(), timeout=5.0)
        except Exception:
            return False
        return True
