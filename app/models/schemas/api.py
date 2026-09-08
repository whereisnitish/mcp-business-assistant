"""API request and response schemas.

Separate from the domain DTOs on purpose. These are the project's *public contract*:
they can carry presentation concerns, and they must stay stable even when internal
models change. Returning ORM objects (or internal DTOs) from an endpoint couples the
wire format to the database schema and leaks fields nobody meant to publish.

Every response model carries an example so the generated OpenAPI docs at ``/docs``
are usable as documentation rather than just a field list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.database.enums import (
    ApprovalStatus,
    ConversationStatus,
    MessageRole,
    PermissionLevel,
)


class APIModel(BaseModel):
    """Base for every API schema."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ErrorDetail(APIModel):
    code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable, safe-to-display description.")
    details: dict[str, Any] | None = Field(default=None, description="Structured context.")


class ErrorResponse(APIModel):
    error: ErrorDetail
    request_id: str | None = Field(
        default=None, description="Correlation id; quote it when reporting a problem."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "tool_permission_denied",
                    "message": "Tool 'crm__create_lead' was blocked by policy.",
                    "details": {
                        "tool": "crm__create_lead",
                        "reason": "Your role only permits read-only tools.",
                    },
                },
                "request_id": "6f1c2a8e9b3d4f5a",
            }
        }
    )


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class ChatRequest(APIModel):
    message: str = Field(
        min_length=1, max_length=8000, description="What you want the assistant to do."
    )
    conversation_id: str | None = Field(
        default=None, description="Continue an existing conversation. Omit to start a new one."
    )

    model_config = ConfigDict(
        json_schema_extra={"example": {"message": "Send a follow-up email to our qualified leads."}}
    )


class PendingApprovalOut(APIModel):
    """A high-risk action waiting for a human decision."""

    approval_id: str
    tool_name: str
    summary: str = Field(description="Exactly what will happen if you confirm.")
    risk_reason: str = Field(description="Why this needs your approval.")
    expires_at: datetime
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="The precise arguments that will be executed."
    )


class ChatResponse(APIModel):
    conversation_id: str
    response: str = Field(description="The assistant's reply.")
    status: Literal["completed", "awaiting_approval", "failed"] = Field(
        description="'awaiting_approval' means nothing was executed; confirm to proceed."
    )
    approval_required: bool = False
    approval: PendingApprovalOut | None = None
    tools_called: list[str] = Field(default_factory=list, description="Tools invoked, in order.")
    errors: list[str] = Field(default_factory=list)
    iterations: int = Field(default=0, description="Plan/act cycles used.")
    duration_ms: float = 0.0

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conversation_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "response": "I prepared an email to 3 qualified leads. Approval required.",
                "status": "awaiting_approval",
                "approval_required": True,
                "approval": {
                    "approval_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
                    "tool_name": "email__send_email",
                    "summary": "Send an email to 3 recipient(s): ada@acme.io, ...",
                    "risk_reason": "Sending an email is irreversible and externally visible.",
                    "expires_at": "2026-09-08T14:30:00Z",
                    "arguments": {
                        "to": ["ada@acme.io"],
                        "subject": "Following up",
                        "body": "Hi ...",
                    },
                },
                "tools_called": ["crm__get_leads", "email__draft_email", "email__send_email"],
                "errors": [],
                "iterations": 3,
                "duration_ms": 412.7,
            }
        }
    )


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
class ApprovalOut(APIModel):
    id: str
    conversation_id: str
    tool_name: str
    server_name: str
    permission_level: PermissionLevel
    status: ApprovalStatus
    summary: str
    risk_reason: str
    arguments: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    error: str | None = None


class ApprovalListResponse(APIModel):
    approvals: list[ApprovalOut]
    count: int


class ApprovalDecisionRequest(APIModel):
    note: str | None = Field(
        default=None, max_length=1000, description="Optional note recorded with the decision."
    )

    model_config = ConfigDict(
        json_schema_extra={"example": {"note": "Checked the recipient list."}}
    )


class ApprovalDecisionResponse(APIModel):
    """Result of confirming or rejecting an approval."""

    approval: ApprovalOut
    executed: bool = Field(description="Whether the approved action actually ran.")
    conversation_id: str
    response: str | None = Field(
        default=None, description="The assistant's message after the action completed."
    )
    error: str | None = None


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
class MessageOut(APIModel):
    id: str
    sequence: int
    role: MessageRole
    content: str | None = None
    tool_name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    created_at: datetime


class ConversationOut(APIModel):
    id: str
    title: str | None = None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationListResponse(APIModel):
    conversations: list[ConversationOut]
    count: int


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
class ToolOut(APIModel):
    name: str = Field(description="Qualified name, '<server>__<tool>'.")
    server: str
    description: str
    permission: PermissionLevel
    requires_approval: bool = Field(description="Whether a human must confirm before this runs.")
    input_schema: dict[str, Any] = Field(description="JSON Schema for the tool's arguments.")
    server_read_only_hint: bool | None = Field(
        default=None,
        description=(
            "The MCP server's own advisory annotation. Informational only -- the "
            "'permission' field above is the authoritative, locally-enforced classification."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "email__send_email",
                "server": "email",
                "description": "Send an email. This is irreversible ...",
                "permission": "high_risk",
                "requires_approval": True,
                "input_schema": {"type": "object", "properties": {"to": {"type": "array"}}},
                "server_read_only_hint": False,
            }
        }
    )


class ToolListResponse(APIModel):
    tools: list[ToolOut]
    count: int
    servers: list[str] = Field(description="MCP servers currently connected.")
    counts_by_permission: dict[str, int]
    degraded: bool = Field(description="True when one or more MCP servers are unreachable.")


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
class HealthResponse(APIModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    environment: str
    database: bool = Field(description="Whether the database answered a trivial query.")
    llm_provider: str
    llm_is_mock: bool = Field(description="True when responses come from a development stand-in.")
    mcp: dict[str, Any] = Field(
        default_factory=dict, description="Per-server MCP connection state."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "version": "1.0.0",
                "environment": "local",
                "database": True,
                "llm_provider": "heuristic",
                "llm_is_mock": True,
                "mcp": {"degraded": False, "tool_count": 17, "servers": {}},
            }
        }
    )
