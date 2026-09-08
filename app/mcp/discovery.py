"""Dynamic tool discovery.

This module is where the value of MCP becomes concrete. The agent contains **no
list of tools**. At startup (and on demand afterwards) the manager asks every
connected server "what can you do?", and the answers -- names, descriptions and
JSON Schemas -- become the catalog the agent plans against.

The practical consequence: adding ``crm__merge_duplicates`` to the CRM server and
restarting makes it available to the agent immediately. No prompt is edited, no
dispatch table is extended, no agent code changes. The only edit required elsewhere
is one line in :mod:`app.security.permissions` to classify it -- and forgetting even
that is safe, because unregistered tools default to HIGH_RISK.

Discovery is also where each tool receives its permission classification, from the
**local** registry rather than from the server's own annotations.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from app.core.logging import get_logger, safe_extra
from app.mcp.types import ToolSpec, qualify
from app.models.database.enums import PermissionLevel
from app.security import permissions

logger = get_logger(__name__)


def build_tool_spec(server: str, tool: Any) -> ToolSpec:
    """Convert an SDK ``Tool`` into a :class:`ToolSpec`, classifying it locally.

    A tool with no description is still usable but much harder for a model to
    choose correctly, so the gap is logged rather than quietly filled in.
    """
    qualified_name = qualify(server, tool.name)
    annotations = getattr(tool, "annotations", None)

    description = (tool.description or "").strip()
    if not description:
        logger.warning(
            "MCP tool has no description; the agent will struggle to select it",
            extra=safe_extra({"event": "mcp.tool_missing_description", "tool": qualified_name}),
        )
        description = f"{tool.name} (no description provided by the server)"

    if not permissions.is_registered(qualified_name):
        logger.warning(
            "MCP tool is not in the permission registry; treating it as high risk",
            extra=safe_extra({"event": "mcp.tool_unclassified", "tool": qualified_name}),
        )

    spec = ToolSpec(
        qualified_name=qualified_name,
        name=tool.name,
        server=server,
        title=getattr(tool, "title", None),
        description=description,
        input_schema=dict(tool.input_schema or {"type": "object", "properties": {}}),
        output_schema=dict(tool.output_schema) if getattr(tool, "output_schema", None) else None,
        permission=permissions.classify(qualified_name),
        server_read_only_hint=getattr(annotations, "read_only_hint", None) if annotations else None,
        server_destructive_hint=getattr(annotations, "destructive_hint", None)
        if annotations
        else None,
    )

    if spec.hint_conflicts_with_policy:
        logger.warning(
            "MCP server advertises a tool as read-only but local policy classifies it higher; "
            "the local policy wins",
            extra=safe_extra(
                {
                    "event": "mcp.annotation_conflict",
                    "tool": qualified_name,
                    "policy": spec.permission.value,
                }
            ),
        )
    return spec


class ToolCatalog:
    """An immutable snapshot of everything discovered across all servers."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._by_name: dict[str, ToolSpec] = {spec.qualified_name: spec for spec in specs}

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._by_name.values())

    def __contains__(self, qualified_name: object) -> bool:
        return qualified_name in self._by_name

    def get(self, qualified_name: str) -> ToolSpec | None:
        return self._by_name.get(qualified_name)

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)

    @property
    def servers(self) -> list[str]:
        return sorted({spec.server for spec in self._by_name.values()})

    def by_server(self, server: str) -> list[ToolSpec]:
        return [spec for spec in self._by_name.values() if spec.server == server]

    def by_permission(self, level: PermissionLevel) -> list[ToolSpec]:
        return [spec for spec in self._by_name.values() if spec.permission == level]

    def readable_only(self) -> ToolCatalog:
        """A catalog restricted to READ tools.

        Used to constrain what a ``VIEWER`` may reach: rather than letting the model
        propose a write and rejecting it afterwards, the write tools are never put
        in front of it. Enforcement still happens at dispatch -- this only avoids
        offering capabilities the user could not use.
        """
        return ToolCatalog(self.by_permission(PermissionLevel.READ))

    def describe(self) -> list[str]:
        """One line per tool, for logging and debugging."""
        return [
            spec.summary()
            for spec in sorted(self._by_name.values(), key=lambda s: s.qualified_name)
        ]

    def stats(self) -> dict[str, int]:
        """Tool counts by permission level, for the health endpoint."""
        counts = {level.value: 0 for level in PermissionLevel}
        for spec in self._by_name.values():
            counts[spec.permission.value] += 1
        counts["total"] = len(self._by_name)
        return counts
