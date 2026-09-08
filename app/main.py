"""FastAPI application factory and lifespan management.

Startup order matters and is deliberate:

1. configure logging, so every later line is structured and correlated;
2. create the schema (development convenience) and optionally seed demo data;
3. build the LLM provider;
4. connect the MCP servers and run tool discovery.

MCP connections are opened **once**, in the lifespan, and shared by every request.
Each stdio server is a subprocess; opening them per request would spawn five
processes per call and pay several seconds of interpreter start-up each time.

The lifespan also owns shutdown: subprocesses are terminated and the database engine
disposed, so a reload or container stop does not leak processes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.routes import approvals, chat, conversations, health, tools
from app.core.config import Settings, get_settings
from app.core.logging import (
    bind_request_context,
    configure_logging,
    get_logger,
    new_request_id,
    reset_request_context,
    safe_extra,
)
from app.db.session import create_all, dispose_engine
from app.mcp.manager import MCPManager
from app.providers.llm.factory import build_llm_provider

logger = get_logger(__name__)

DESCRIPTION = """\
An AI business assistant that operates real business tools through the
**Model Context Protocol**.

The agent does not contain a hard-coded list of tools. It discovers them at runtime
from connected MCP servers (CRM, tasks, spreadsheets, email, calendar) and plans
against whatever it finds.

**Security model.** Every tool carries a permission level assigned by this service,
not by the MCP server that provides it:

* `read` -- runs automatically;
* `write` -- runs automatically unless the deployment requires confirmation;
* `high_risk` -- never runs without explicit human approval.

A high-risk request returns `approval_required` with the exact arguments awaiting
execution. Nothing runs until a human confirms via
`POST /api/v1/approvals/{id}/confirm`. The language model cannot bypass this: the
decision is made in backend code and is not reachable from model output.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage process-lifetime resources."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    logger.info(
        "starting application",
        extra=safe_extra(
            {
                "event": "app.starting",
                "version": __version__,
                "environment": settings.environment,
                "llm_provider": settings.llm_provider,
                "mcp_transport": settings.mcp_transport,
            }
        ),
    )
    _warn_about_insecure_configuration(settings)

    if settings.auto_create_schema:
        await create_all(settings)
    if settings.seed_demo_data:
        from app.db.seed import seed_demo_data

        await seed_demo_data(settings)

    app.state.llm_provider = build_llm_provider(settings)

    manager = MCPManager(settings=settings)
    app.state.mcp_manager = manager
    try:
        await manager.startup()
    except Exception:
        # With MCP_FAIL_FAST=false the manager already degrades internally; reaching
        # here means fail-fast was requested, or something unexpected broke.
        logger.exception(
            "MCP startup failed", extra=safe_extra({"event": "app.mcp_startup_failed"})
        )
        await manager.shutdown()
        raise

    logger.info(
        "application ready",
        extra=safe_extra(
            {
                "event": "app.ready",
                "tool_count": len(manager.catalog),
                "degraded": manager.is_degraded,
            }
        ),
    )

    try:
        yield
    finally:
        logger.info("shutting down", extra=safe_extra({"event": "app.stopping"}))
        await manager.shutdown()
        provider = getattr(app.state, "llm_provider", None)
        if provider is not None:
            await provider.aclose()
        await dispose_engine()


def _warn_about_insecure_configuration(settings: Settings) -> None:
    """Make risky-but-convenient defaults loud outside local development."""
    if not settings.auth_enabled and settings.environment not in ("local", "test"):
        logger.warning(
            "AUTH_ENABLED is false outside local development; every request is attributed "
            "to the demo user and the API is effectively unauthenticated",
            extra=safe_extra({"event": "app.auth_disabled"}),
        )
    if settings.debug and settings.is_production:
        logger.warning(
            "DEBUG is enabled in production", extra=safe_extra({"event": "app.debug_in_production"})
        )
    if "*" in settings.cors_origins and settings.is_production:
        logger.warning(
            "CORS is open to all origins in production",
            extra=safe_extra({"event": "app.cors_wildcard"}),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level singleton, so tests can build an app with
    different settings without reimporting the module.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "MCP Business Assistant"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        """Assign a request id, bind it to the logging context, and time the request.

        An inbound ``X-Request-ID`` is honoured so a trace can span an upstream
        gateway; otherwise one is generated. It is echoed on the response, which is
        what lets a user quote an id from an error and have it match the logs.
        """
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        tokens = bind_request_context(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            reset_request_context(tokens)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(duration_ms)

        if request.url.path not in ("/health/live", "/metrics"):
            logger.info(
                "request completed",
                extra=safe_extra(
                    {
                        "event": "http.request",
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                        "request_id": request_id,
                    }
                ),
            )
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    prefix = settings.api_v1_prefix
    app.include_router(chat.router, prefix=prefix)
    app.include_router(approvals.router, prefix=prefix)
    app.include_router(conversations.router, prefix=prefix)
    app.include_router(tools.router, prefix=prefix)

    _mount_web_console(app, settings)

    return app


def _mount_web_console(app: FastAPI, settings: Settings) -> None:
    """Serve the single-page console at ``/``.

    A small operator UI over the same public API documented at ``/docs`` -- it uses
    no private endpoint, so it cannot drift from the backend contract.

    Mounting is conditional: if the directory is absent (a trimmed deployment that
    ships only the API, say) the service still starts and ``/`` returns the JSON
    descriptor instead. A missing static directory should not take an API down.
    """
    static_dir = Path(__file__).parent / "static"
    index_file = static_dir / "index.html"

    descriptor = {
        "name": settings.app_name,
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "tools": f"{settings.api_v1_prefix}/tools",
    }

    if not index_file.is_file():
        logger.info(
            "web console not bundled; serving the JSON descriptor at /",
            extra=safe_extra({"event": "app.no_web_console"}),
        )

        @app.get("/", include_in_schema=False)
        async def root_descriptor() -> dict[str, str]:
            return descriptor

        return

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def web_console() -> FileResponse:
        # no-store: the console is one file with no content hash in its name, so a
        # cached copy would silently survive a redeploy.
        return FileResponse(index_file, headers={"Cache-Control": "no-store"})

    @app.get("/api", include_in_schema=False)
    async def root_json() -> dict[str, str]:
        """The JSON descriptor that used to live at ``/``."""
        return descriptor


app = create_app()
