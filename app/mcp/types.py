"""Value types for the MCP client layer.

These are the vocabulary the rest of the application speaks. Nothing above this
module imports the MCP SDK directly: the agent, the policy engine and the API all
work with :class:`ToolSpec`, :class:`ToolCall` and :class:`ToolResult`. That keeps
the SDK's surface -- which changed substantially between its v1 and v2 releases --
behind one seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.database.enums import PermissionLevel

#: Separator between the server name and the tool name in a qualified tool name.
#: Two underscores, because the resulting identifier must match the
#: ``^[a-zA-Z0-9_-]+$`` pattern that OpenAI-compatible function names require --
#: a dot would be rejected -- and a single underscore could not be
#: unambiguously split from tool names that already contain one.
NAME_SEPARATOR = "__"


def qualify(server: str, tool: str) -> str:
    """Build the globally unique name for a tool: ``crm__get_leads``."""
    return f"{server}{NAME_SEPARATOR}{tool}"


def split_qualified(qualified_name: str) -> tuple[str, str]:
    """Split a qualified name back into ``(server, tool)``.

    Raises:
        ValueError: if the name is not qualified. Callers treat this as "unknown
            tool" rather than guessing at a server, so an unqualified name from a
            model can never be silently routed somewhere.
    """
    server, separator, tool = qualified_name.partition(NAME_SEPARATOR)
    if not separator or not server or not tool:
        raise ValueError(
            f"{qualified_name!r} is not a qualified tool "
            f"name; expected '<server>{NAME_SEPARATOR}<tool>'."
        )
    return server, tool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool discovered from an MCP server, as the application sees it.

    Attributes:
        qualified_name: Unique name used everywhere outside the owning server.
        name: The tool's local name on its server, used on the wire.
        server: Name of the MCP server that provides it.
        description: Natural-language description, shown to the model.
        input_schema: JSON Schema for the arguments.
        output_schema: JSON Schema for a structured result, when the server offers one.
        permission: Risk classification assigned by the **local** policy registry.
        server_read_only_hint: The server's own ``read_only_hint`` annotation.
            Advisory and display-only -- see :attr:`hint_conflicts_with_policy`.
        server_destructive_hint: The server's own ``destructive_hint`` annotation.
    """

    qualified_name: str
    name: str
    server: str
    description: str
    input_schema: dict[str, Any]
    permission: PermissionLevel
    output_schema: dict[str, Any] | None = None
    title: str | None = None
    server_read_only_hint: bool | None = None
    server_destructive_hint: bool | None = None

    @property
    def hint_conflicts_with_policy(self) -> bool:
        """True when the server claims a tool is read-only but policy says otherwise.

        Worth surfacing: a server advertising ``read_only_hint=True`` for something
        the local registry classifies as WRITE or HIGH_RISK is either misconfigured
        or hostile. The policy wins either way -- this flag exists so the mismatch
        is visible on ``/tools`` and in the audit trail rather than silent.
        """
        return bool(self.server_read_only_hint) and self.permission != PermissionLevel.READ

    def summary(self) -> str:
        """One-line description used in prompts and log lines."""
        return f"{self.qualified_name} [{self.permission.value}] - {self.description}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation proposed by the agent (or replayed from an approval)."""

    id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def server(self) -> str:
        return split_qualified(self.tool_name)[0]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of a tool call.

    A failed call is represented as a value, not an exception: the agent needs to
    read the error and decide what to do next, and a raised exception would end the
    graph run instead of feeding the model something it can recover from.
    """

    tool_call_id: str
    tool_name: str
    success: bool
    content: str = ""
    structured_content: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0
    server: str | None = None

    @classmethod
    def ok(
        cls,
        call: ToolCall,
        *,
        content: str,
        structured_content: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
    ) -> ToolResult:
        return cls(
            tool_call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            content=content,
            structured_content=structured_content,
            duration_ms=duration_ms,
            server=_safe_server(call.tool_name),
        )

    @classmethod
    def failure(cls, call: ToolCall, error: str, *, duration_ms: float = 0.0) -> ToolResult:
        return cls(
            tool_call_id=call.id,
            tool_name=call.tool_name,
            success=False,
            content=error,
            error=error,
            duration_ms=duration_ms,
            server=_safe_server(call.tool_name),
        )

    def to_payload(self) -> dict[str, Any]:
        """Compact representation for audit rows and conversation history."""
        payload: dict[str, Any] = {
            "tool": self.tool_name,
            "success": self.success,
            "duration_ms": self.duration_ms,
        }
        if self.structured_content is not None:
            payload["result"] = self.structured_content
        elif self.content:
            payload["result"] = self.content
        if self.error:
            payload["error"] = self.error
        return payload


def _safe_server(qualified_name: str) -> str | None:
    try:
        return split_qualified(qualified_name)[0]
    except ValueError:
        return None
