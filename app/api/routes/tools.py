"""Tool catalog endpoint.

Exposes what the agent discovered from the connected MCP servers, together with the
permission level the backend assigned to each tool. Useful for demonstrating the
point of MCP: this list is not written anywhere in the application: it is whatever
the servers reported at runtime.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.dependencies import AppSettings, CurrentUser, Manager
from app.core.config import Settings
from app.models.database.enums import PermissionLevel
from app.models.schemas.api import ToolListResponse, ToolOut
from app.security import permissions

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get(
    "",
    response_model=ToolListResponse,
    summary="List the MCP tools currently available",
)
async def list_tools(
    user: CurrentUser,
    manager: Manager,
    settings: AppSettings,
    permission: PermissionLevel | None = Query(
        default=None, description="Filter to a single permission level."
    ),
    refresh: bool = Query(
        default=False, description="Re-run discovery against the servers before answering."
    ),
) -> ToolListResponse:
    """Return the discovered tool catalog.

    ``permission`` is this application's authoritative classification. A tool may
    also carry ``server_read_only_hint`` -- the MCP server's own advisory annotation
    -- which is reported for transparency but never used to authorise anything.
    """
    if refresh:
        await manager.refresh_catalog()

    catalog = manager.catalog
    specs = catalog.by_permission(permission) if permission else list(catalog)

    return ToolListResponse(
        tools=[
            ToolOut(
                name=spec.qualified_name,
                server=spec.server,
                description=spec.description,
                permission=spec.permission,
                requires_approval=_requires_approval(spec.permission, settings),
                input_schema=spec.input_schema,
                server_read_only_hint=spec.server_read_only_hint,
            )
            for spec in sorted(specs, key=lambda item: item.qualified_name)
        ],
        count=len(specs),
        servers=catalog.servers,
        counts_by_permission=catalog.stats(),
        degraded=manager.is_degraded,
    )


@router.get(
    "/permissions",
    summary="Explain the permission model",
)
async def describe_permissions(user: CurrentUser) -> dict[str, object]:
    """Describe each permission level and which tools sit at it.

    Documents the security model as it is actually configured, rather than as a
    README claims it to be.
    """
    return {
        "levels": {
            level.value: {
                "description": permissions.describe(level),
                "tools": permissions.tools_at_level(level),
            }
            for level in PermissionLevel
        },
        "default_for_unregistered_tools": permissions.DEFAULT_PERMISSION.value,
        "note": (
            "Any tool absent from the registry is treated as high risk and requires human "
            "approval. Permission levels are enforced by the API process, not by the MCP "
            "servers, and cannot be influenced by the language model."
        ),
    }


def _requires_approval(level: PermissionLevel, settings: Settings) -> bool:
    """Whether a tool at this level will pause for approval in this deployment.

    HIGH_RISK always does; WRITE does only when the deployment opts in via
    ``REQUIRE_APPROVAL_FOR_WRITES``.
    """
    if level == PermissionLevel.HIGH_RISK:
        return True
    return level == PermissionLevel.WRITE and settings.require_approval_for_writes
