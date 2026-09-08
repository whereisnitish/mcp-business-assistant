"""Test data builders.

Small helpers that keep tests focused on the behaviour under test rather than on
assembling valid domain objects.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.mcp.types import ToolSpec
from app.models.database.enums import PermissionLevel, UserRole
from app.security.policy import PolicyContext

#: Minimal object schema used where the schema itself is not under test.
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def make_tool_spec(
    qualified_name: str = "crm__get_leads",
    *,
    permission: PermissionLevel = PermissionLevel.READ,
    input_schema: dict[str, Any] | None = None,
    description: str = "A test tool.",
    server_read_only_hint: bool | None = None,
) -> ToolSpec:
    server, _, name = qualified_name.partition("__")
    return ToolSpec(
        qualified_name=qualified_name,
        name=name,
        server=server,
        description=description,
        input_schema=input_schema or EMPTY_SCHEMA,
        permission=permission,
        server_read_only_hint=server_read_only_hint,
    )


def make_policy_context(
    *,
    role: UserRole = UserRole.OPERATOR,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> PolicyContext:
    return PolicyContext(
        user_id=user_id or str(uuid.uuid4()),
        user_role=role,
        conversation_id=conversation_id or str(uuid.uuid4()),
    )


def future(minutes: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def past(minutes: int = 30) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


SEND_EMAIL_ARGS: dict[str, Any] = {
    "to": ["ada@example.com"],
    "subject": "Following up",
    "body": "Hello there.",
}
