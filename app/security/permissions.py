"""Tool permission registry -- the authoritative risk classification.

This registry is the security backbone of the project, and three properties matter
more than the table itself:

**1. It lives in the API process, not in the MCP servers.**
An MCP server can advertise ``read_only_hint=True`` for a tool that deletes your
database. Those annotations arrive over the wire from whoever is running the
server, so treating them as an authorisation decision would mean delegating
authorisation to the thing being authorised. They are recorded for display and
compared against this registry (:attr:`ToolSpec.hint_conflicts_with_policy`), never
trusted.

**2. It fails closed.**
A tool that is not listed here is classified HIGH_RISK and therefore requires human
approval. Connect a new MCP server and its tools are usable but gated, rather than
silently granted the permissions of whatever the model felt like calling. The cost
of forgetting an entry is an unnecessary approval prompt, not an unreviewed action.

**3. The model has no influence over it.**
Classification is a dictionary lookup on the tool name. There is no path by which
prompt text, tool output or conversation history can change the result -- which is
what makes the guarantee survive prompt injection.
"""

from __future__ import annotations

from typing import Final

from app.mcp.types import qualify
from app.models.database.enums import PermissionLevel

#: Applied to any tool absent from the registry. Fail closed, always.
DEFAULT_PERMISSION: Final[PermissionLevel] = PermissionLevel.HIGH_RISK

#: Authoritative classification, keyed by qualified tool name.
#:
#: READ       -- observes state; safe to run without asking.
#: WRITE      -- changes internal business state; reversible by a human.
#: HIGH_RISK  -- irreversible, externally visible, or destructive. Always approved.
TOOL_PERMISSIONS: Final[dict[str, PermissionLevel]] = {
    # --- CRM ---------------------------------------------------------------- #
    qualify("crm", "get_leads"): PermissionLevel.READ,
    qualify("crm", "get_lead"): PermissionLevel.READ,
    qualify("crm", "search_leads"): PermissionLevel.READ,
    qualify("crm", "create_lead"): PermissionLevel.WRITE,
    qualify("crm", "update_lead_status"): PermissionLevel.WRITE,
    # --- Tasks -------------------------------------------------------------- #
    qualify("tasks", "get_tasks"): PermissionLevel.READ,
    qualify("tasks", "get_overdue_tasks"): PermissionLevel.READ,
    qualify("tasks", "create_task"): PermissionLevel.WRITE,
    qualify("tasks", "complete_task"): PermissionLevel.WRITE,
    # --- Spreadsheets / sales ----------------------------------------------- #
    qualify("spreadsheets", "get_sales_summary"): PermissionLevel.READ,
    qualify("spreadsheets", "generate_weekly_report"): PermissionLevel.READ,
    qualify("spreadsheets", "add_sales_record"): PermissionLevel.WRITE,
    # --- Calendar ------------------------------------------------------------ #
    qualify("calendar", "get_todays_meetings"): PermissionLevel.READ,
    qualify("calendar", "get_events"): PermissionLevel.READ,
    # Booking a meeting notifies attendees, so it is not merely an internal write.
    qualify("calendar", "create_event"): PermissionLevel.HIGH_RISK,
    # --- Email --------------------------------------------------------------- #
    # Drafting is safe by construction: it composes text and sends nothing.
    qualify("email", "draft_email"): PermissionLevel.READ,
    # Sending leaves the building and cannot be recalled.
    qualify("email", "send_email"): PermissionLevel.HIGH_RISK,
}

#: Why a tool carries its classification. Shown to the human approver, so it is
#: written for a person deciding whether to allow an action -- not for a developer.
RISK_REASONS: Final[dict[str, str]] = {
    qualify("email", "send_email"): (
        "Sending an email is irreversible and visible to people outside the company."
    ),
    qualify("calendar", "create_event"): (
        "Booking a meeting places an entry on other people's calendars and notifies attendees."
    ),
    qualify("crm", "create_lead"): "This adds a new record to the CRM.",
    qualify("crm", "update_lead_status"): "This changes a lead's stage in the sales pipeline.",
    qualify("tasks", "create_task"): "This creates a task others may act on.",
    qualify("tasks", "complete_task"): "This marks work as finished.",
    qualify("spreadsheets", "add_sales_record"): "This writes a new row into the sales records.",
}

#: Plain-language meaning of each level, surfaced through the API.
PERMISSION_DESCRIPTIONS: Final[dict[PermissionLevel, str]] = {
    PermissionLevel.READ: "Reads data without changing anything. Runs automatically.",
    PermissionLevel.WRITE: (
        "Changes business data inside the system. Runs automatically unless "
        "REQUIRE_APPROVAL_FOR_WRITES is enabled."
    ),
    PermissionLevel.HIGH_RISK: (
        "Irreversible or externally visible. Always requires explicit human approval."
    ),
}


def classify(qualified_name: str) -> PermissionLevel:
    """Return the permission level for *qualified_name*.

    Unknown tools are HIGH_RISK. This is the single function every other component
    consults; there is deliberately no override parameter and no caller-supplied
    default, so no call site can weaken the classification locally.
    """
    return TOOL_PERMISSIONS.get(qualified_name, DEFAULT_PERMISSION)


def is_registered(qualified_name: str) -> bool:
    """Whether the tool has an explicit classification.

    A ``False`` here means the tool is being treated as HIGH_RISK by default; the
    discovery layer logs this so an unregistered tool is noticed rather than merely
    tolerated.
    """
    return qualified_name in TOOL_PERMISSIONS


def risk_reason(qualified_name: str) -> str:
    """Human-readable justification for a tool's classification."""
    if reason := RISK_REASONS.get(qualified_name):
        return reason
    if not is_registered(qualified_name):
        return (
            "This tool is not in the permission registry, so it is treated as high risk "
            "until it has been reviewed and classified."
        )
    return PERMISSION_DESCRIPTIONS[classify(qualified_name)]


def describe(level: PermissionLevel) -> str:
    """Plain-language description of a permission level."""
    return PERMISSION_DESCRIPTIONS[level]


def tools_at_level(level: PermissionLevel) -> list[str]:
    """Every registered tool at a given level. Used by tests and documentation."""
    return sorted(name for name, value in TOOL_PERMISSIONS.items() if value == level)
