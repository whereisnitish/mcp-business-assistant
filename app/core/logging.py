"""Structured logging with request-scoped context.

Log records are emitted as single-line JSON so they can be shipped to any log
aggregator without a parsing rule. Correlation identifiers (request id, conversation
id, user id) live in :mod:`contextvars`, which means they propagate automatically
through the async call chain -- an agent node three awaits deep does not need to
thread a request id through its signature to have it appear in its logs.

Every structured field passes through :func:`app.core.security.redact` before it is
serialised, so a credential can never reach a log line by accident.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from app.core.security import redact, truncate

# --------------------------------------------------------------------------- #
# Request-scoped context
# --------------------------------------------------------------------------- #
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

#: Attributes present on every stdlib LogRecord; anything else was supplied by the
#: caller as structured context and is merged into the JSON payload.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


#: Names the stdlib refuses to accept in ``extra=`` because they would clobber a
#: LogRecord attribute. ``logging`` raises ``KeyError`` rather than dropping them,
#: so a log call using a natural field name like ``args`` or ``module`` would crash
#: the caller. :func:`safe_extra` renames them instead.
_PROTECTED_EXTRA_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Make a structured-context mapping safe to pass as ``logging`` ``extra=``.

    Keys that would collide with a stdlib ``LogRecord`` attribute are prefixed with
    ``ctx_`` so that logging a field genuinely called ``args`` or ``module`` cannot
    raise. Always route caller-supplied field names through this.
    """
    return {
        (f"ctx_{key}" if key in _PROTECTED_EXTRA_KEYS else key): value
        for key, value in fields.items()
    }


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_context(
    *,
    request_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Token[Any]]:
    """Bind correlation ids to the current context, returning reset tokens."""
    tokens: dict[str, Token[Any]] = {}
    if request_id is not None:
        tokens["request_id"] = request_id_var.set(request_id)
    if conversation_id is not None:
        tokens["conversation_id"] = conversation_id_var.set(conversation_id)
    if user_id is not None:
        tokens["user_id"] = user_id_var.set(user_id)
    return tokens


def reset_request_context(tokens: dict[str, Token[Any]]) -> None:
    """Undo a previous :func:`bind_request_context`."""
    for name, token in tokens.items():
        {
            "request_id": request_id_var,
            "conversation_id": conversation_id_var,
            "user_id": user_id_var,
        }[name].reset(token)


def current_request_id() -> str | None:
    return request_id_var.get()


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with redacted structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": truncate(record.getMessage(), 8000),
        }

        for key, value in (
            (k, v) for k, v in record.__dict__.items() if k not in _RESERVED_RECORD_ATTRS
        ):
            payload[key] = truncate(redact(value))

        for name, var in (
            ("request_id", request_id_var),
            ("conversation_id", conversation_id_var),
            ("user_id", user_id_var),
        ):
            value = var.get()
            if value is not None:
                payload.setdefault(name, value)

        if record.exc_info:
            # The formatted traceback stays in the log stream (operators need it) but
            # is redacted and never propagates to an API response.
            payload["exception"] = truncate(redact(self.formatException(record.exc_info)), 8000)

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter for local development."""

    _BASE = "%(asctime)s %(levelname)-8s %(name)-34s %(message)s"

    def __init__(self) -> None:
        super().__init__(self._BASE, datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: redact(value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS
        }
        request_id = request_id_var.get()
        if request_id:
            extras.setdefault("request_id", request_id[:8])
        if extras:
            rendered = " ".join(
                f"{k}={json.dumps(v, default=str)}" for k, v in sorted(extras.items())
            )
            return f"{base}  |  {rendered}"
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the root logging configuration. Safe to call more than once.

    MCP servers speak JSON-RPC over **stdout** when using the stdio transport, so
    every log record goes to stderr. Writing a log line to stdout in a stdio server
    corrupts the protocol stream.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; route them through ours instead.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
class Timer:
    """Monotonic stopwatch used to attach ``duration_ms`` to logs and audit rows."""

    __slots__ = ("_elapsed", "_start")

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._elapsed: float | None = None

    def stop(self) -> float:
        self._elapsed = time.perf_counter() - self._start
        return self._elapsed

    @property
    def elapsed_ms(self) -> float:
        elapsed = self._elapsed if self._elapsed is not None else time.perf_counter() - self._start
        return round(elapsed * 1000, 2)


@contextmanager
def log_duration(logger: logging.Logger, event: str, **fields: Any) -> Iterator[Timer]:
    """Log the duration of a block, whether or not it raises.

    Example:
        >>> with log_duration(log, "tool.execute", tool="crm__get_leads"):
        ...     ...
    """
    timer = Timer()
    try:
        yield timer
    except Exception as exc:
        logger.warning(
            "%s failed",
            event,
            extra=safe_extra(
                {
                    **fields,
                    "event": event,
                    "duration_ms": timer.elapsed_ms,
                    "error_type": type(exc).__name__,
                }
            ),
        )
        raise
    else:
        logger.info(
            "%s completed",
            event,
            extra=safe_extra({**fields, "event": event, "duration_ms": timer.elapsed_ms}),
        )
