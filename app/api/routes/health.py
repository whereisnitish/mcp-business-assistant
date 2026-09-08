"""Health endpoint.

Reports *dependency* health, not merely process liveness. A container that responds
"ok" while its database is unreachable and every MCP server is down is worse than
one that fails its health check, because an orchestrator will happily keep routing
traffic to it.

``status`` is:

* ``ok``       -- database reachable and every MCP server connected;
* ``degraded`` -- serving requests with reduced capability (some servers down);
* ``error``    -- the database is unreachable, so nothing meaningful can be served.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app import __version__
from app.api.dependencies import LLM, AppSettings, DBSession, Manager
from app.core.logging import get_logger
from app.models.schemas.api import HealthResponse

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service and dependency health",
    responses={
        503: {"model": HealthResponse, "description": "A required dependency is unavailable"}
    },
)
async def health(
    response: Response,
    session: DBSession,
    manager: Manager,
    llm: LLM,
    settings: AppSettings,
) -> HealthResponse:
    """Check the database and the MCP layer."""
    database_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
        logger.exception("database health check failed", extra={"event": "health.db_failed"})

    mcp_health = await manager.health()

    if not database_ok:
        overall = "error"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif mcp_health.get("degraded"):
        overall = "degraded"
        # Still a 200: the service is usable, just not at full capability. A 503
        # here would take a partially-working assistant out of rotation entirely.
        response.status_code = status.HTTP_200_OK
    else:
        overall = "ok"

    return HealthResponse(
        status=overall,  # type: ignore[arg-type]
        version=__version__,
        environment=settings.environment,
        database=database_ok,
        llm_provider=llm.name,
        llm_is_mock=llm.is_mock,
        mcp=mcp_health,
    )


@router.get("/health/live", summary="Liveness probe", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Cheap liveness probe: is the process running and serving?

    Deliberately checks nothing else. A liveness probe that fails on a transient
    database blip causes the orchestrator to restart a healthy process, which turns
    a brief dependency outage into an outage of your own.
    """
    return {"status": "alive"}
