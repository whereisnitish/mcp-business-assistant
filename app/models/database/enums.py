"""Enumerations shared by the ORM models and the API schemas.

All enums are :class:`enum.StrEnum` so they serialise straight to JSON, and every
column built from them uses :func:`str_enum_column`, which stores the *value*
(``"qualified"``) rather than the member name (``"QUALIFIED"``) and renders as a
``VARCHAR`` + ``CHECK`` constraint instead of a native PostgreSQL ``ENUM`` type.
Native enums require a migration to add a single new member; a check constraint
does not, and it keeps the schema identical on SQLite for tests.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum


def str_enum_column(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Build a portable, value-storing SQL enum for *enum_cls*."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class UserRole(StrEnum):
    """Coarse authorisation role.

    ``VIEWER`` may only trigger read tools; ``OPERATOR`` is the normal business
    user; ``ADMIN`` may additionally approve actions proposed in another user's
    conversation.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_APPROVAL = "awaiting_approval"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    """Mirrors the OpenAI-compatible chat message roles."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    WON = "won"
    LOST = "lost"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class PermissionLevel(StrEnum):
    """Risk classification that drives the approval decision.

    Ordering matters: see :data:`PERMISSION_ORDER`.
    """

    READ = "read"
    WRITE = "write"
    HIGH_RISK = "high_risk"


#: Severity ordering used when a policy needs to compare two levels.
PERMISSION_ORDER: dict[PermissionLevel, int] = {
    PermissionLevel.READ: 0,
    PermissionLevel.WRITE: 1,
    PermissionLevel.HIGH_RISK: 2,
}


class ApprovalStatus(StrEnum):
    """Lifecycle of an approval request.

    Legal transitions are enforced in :mod:`app.services.approval_service`::

        PENDING -> APPROVED -> EXECUTED | FAILED
        PENDING -> REJECTED
        PENDING -> EXPIRED
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class AuditEventType(StrEnum):
    """Every event worth reconstructing after the fact."""

    AGENT_REQUEST_RECEIVED = "agent.request_received"
    AGENT_TOOLS_DISCOVERED = "agent.tools_discovered"
    AGENT_DECISION = "agent.decision"
    AGENT_RESPONSE = "agent.response"
    AGENT_ERROR = "agent.error"
    POLICY_DECISION = "policy.decision"
    TOOL_CALL_STARTED = "tool.call_started"
    TOOL_CALL_SUCCEEDED = "tool.call_succeeded"
    TOOL_CALL_FAILED = "tool.call_failed"
    TOOL_CALL_BLOCKED = "tool.call_blocked"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_CONFIRMED = "approval.confirmed"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_EXECUTED = "approval.executed"
    MCP_SERVER_CONNECTED = "mcp.server_connected"
    MCP_SERVER_FAILED = "mcp.server_failed"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    PENDING = "pending"


class EmailStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    FAILED = "failed"


def enum_values(enum_cls: type[StrEnum]) -> list[Any]:
    """Return the raw values of an enum, for schema documentation."""
    return [member.value for member in enum_cls]
