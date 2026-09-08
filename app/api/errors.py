"""Error handling for the API layer.

One rule: **no internal detail crosses the boundary.**

* An :class:`~app.core.exceptions.AppError` was raised deliberately and carries a
  message written to be read by a client, so it is returned as-is with its code.
* Anything else is a bug. The traceback is logged with the request id and the client
  receives a generic 500 quoting only that id. Stack traces, SQL fragments and
  driver messages routinely contain table names, connection strings or fragments of
  user data -- all of which are useful to an attacker and useless to a caller.

Every response carries ``request_id``, which ties a user-visible failure to the exact
log entries and audit rows for that request.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.core.logging import current_request_id, get_logger, safe_extra
from app.core.security import redact

logger = get_logger(__name__)

#: Starlette renamed 422 in a recent release. The literal is used directly because
#: reading the old alias -- even only as a fallback default -- emits a deprecation
#: warning, since `getattr`'s default argument is evaluated eagerly.
UNPROCESSABLE = 422


def _payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = redact(details)
    body["request_id"] = current_request_id()
    return body


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Render a deliberate application error."""
    logger.warning(
        "request failed with a handled error",
        extra=safe_extra(
            {
                "event": "api.app_error",
                "error_code": exc.code,
                "status_code": exc.status_code,
                "path": request.url.path,
            }
        ),
    )
    return JSONResponse(
        status_code=exc.status_code, content=_payload(exc.code, exc.message, exc.details)
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render a request-body validation failure.

    Field locations and messages are returned (they help the caller fix the request)
    but the submitted values are not echoed, since a rejected payload may contain
    exactly the sensitive data that made it invalid.
    """
    problems = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", ""),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=UNPROCESSABLE,
        content=_payload("validation_error", "The request was not valid.", {"errors": problems}),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render framework-raised HTTP errors (404 on an unknown route, and similar)."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(f"http_{exc.status_code}", str(exc.detail)),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return nothing revealing."""
    logger.exception(
        "unhandled exception while serving a request",
        extra=safe_extra(
            {
                "event": "api.unhandled_error",
                "path": request.url.path,
                "error_type": type(exc).__name__,
            }
        ),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_payload(
            "internal_error",
            "An unexpected error occurred. Quote the request_id when reporting this.",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the application."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)


def encode(model: Any) -> Any:
    """JSON-encode a Pydantic model for a manually constructed response."""
    return jsonable_encoder(model)
