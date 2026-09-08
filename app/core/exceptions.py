"""Application exception hierarchy.

Every error raised deliberately by the application derives from :class:`AppError`.
The API layer converts these into structured JSON responses; anything that is *not*
an ``AppError`` is treated as an unexpected failure and reported as a generic 500
with no internal detail (see :mod:`app.api.errors`).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all deliberate application errors.

    Attributes:
        code: Stable, machine-readable error identifier exposed to API clients.
        status_code: HTTP status the API layer should use.
        message: Human-readable, *client-safe* description. Never embed secrets,
            stack traces or raw upstream payloads here.
        details: Optional structured context, also client-safe.
    """

    code: str = "internal_error"
    status_code: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.details = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# --------------------------------------------------------------------------- #
# Client errors
# --------------------------------------------------------------------------- #
class ValidationError(AppError):
    code = "validation_error"
    status_code = 422
    message = "The request was not valid."


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    message = "The requested resource was not found."


class AuthenticationError(AppError):
    code = "authentication_failed"
    status_code = 401
    message = "Authentication credentials were missing or invalid."


class AuthorizationError(AppError):
    code = "not_authorized"
    status_code = 403
    message = "You are not allowed to perform this action."


class ConflictError(AppError):
    code = "conflict"
    status_code = 409
    message = "The resource is not in a state that allows this operation."


# --------------------------------------------------------------------------- #
# MCP / tooling errors
# --------------------------------------------------------------------------- #
class MCPError(AppError):
    """Base class for failures in the MCP client/server layer."""

    code = "mcp_error"
    status_code = 502
    message = "An MCP operation failed."


class MCPServerUnavailableError(MCPError):
    code = "mcp_server_unavailable"
    status_code = 503
    message = "The MCP server is not available."

    def __init__(self, server: str, reason: str | None = None) -> None:
        details: dict[str, Any] = {"server": server}
        if reason:
            details["reason"] = reason
        super().__init__(f"MCP server {server!r} is unavailable.", details=details)


class ToolNotFoundError(MCPError):
    code = "tool_not_found"
    status_code = 404
    message = "The requested tool is not available."

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool {tool_name!r} is not available.", details={"tool": tool_name})


class ToolExecutionError(MCPError):
    code = "tool_execution_failed"
    status_code = 502
    message = "The tool failed to execute."

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(
            f"Tool {tool_name!r} failed to execute.",
            details={"tool": tool_name, "reason": reason},
        )


class ToolArgumentError(MCPError):
    """The arguments proposed for a tool call did not satisfy its input schema."""

    code = "invalid_tool_arguments"
    status_code = 422
    message = "The tool arguments were invalid."

    def __init__(self, tool_name: str, errors: list[str]) -> None:
        super().__init__(
            f"Invalid arguments for tool {tool_name!r}.",
            details={"tool": tool_name, "errors": errors},
        )


class ToolTimeoutError(MCPError):
    code = "tool_timeout"
    status_code = 504
    message = "The tool call timed out."

    def __init__(self, tool_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Tool {tool_name!r} timed out after {timeout_seconds:g}s.",
            details={"tool": tool_name, "timeout_seconds": timeout_seconds},
        )


# --------------------------------------------------------------------------- #
# Permission / approval errors
# --------------------------------------------------------------------------- #
class PermissionDeniedError(AuthorizationError):
    """A tool call was blocked by the deterministic policy engine."""

    code = "tool_permission_denied"
    status_code = 403

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(
            f"Tool {tool_name!r} was blocked by policy.",
            details={"tool": tool_name, "reason": reason},
        )


class ApprovalRequiredError(AppError):
    """Raised when execution was attempted without a valid approval grant."""

    code = "approval_required"
    status_code = 403
    message = "This action requires human approval before it can be executed."

    def __init__(self, tool_name: str, approval_id: str | None = None) -> None:
        details: dict[str, Any] = {"tool": tool_name}
        if approval_id:
            details["approval_id"] = approval_id
        super().__init__(f"Tool {tool_name!r} requires human approval.", details=details)


class ApprovalExpiredError(ConflictError):
    code = "approval_expired"
    status_code = 409
    message = "The approval request has expired and can no longer be confirmed."


class ApprovalStateError(ConflictError):
    code = "approval_invalid_state"
    status_code = 409
    message = "The approval request has already been decided."


class ApprovalIntegrityError(AppError):
    """The stored approval payload no longer matches its integrity hash.

    This is a tamper signal: the arguments a human approved are not the arguments
    that would be executed. Always fail closed.
    """

    code = "approval_integrity_failure"
    status_code = 409
    message = "The approved action payload failed its integrity check."


# --------------------------------------------------------------------------- #
# LLM / agent errors
# --------------------------------------------------------------------------- #
class LLMError(AppError):
    code = "llm_error"
    status_code = 502
    message = "The language model provider failed."


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    status_code = 504
    message = "The language model provider timed out."


class LLMConfigurationError(AppError):
    code = "llm_not_configured"
    status_code = 500
    message = "The language model provider is not configured correctly."


class AgentError(AppError):
    code = "agent_error"
    status_code = 500
    message = "The agent failed to complete the request."


class AgentIterationLimitError(AgentError):
    code = "agent_iteration_limit"
    status_code = 500
    message = "The agent exceeded its maximum number of reasoning steps."


class DatabaseError(AppError):
    code = "database_error"
    status_code = 500
    message = "A database operation failed."
