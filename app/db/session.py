"""Async engine and session management.

The engine is created lazily and cached per process. Both the FastAPI application
and the MCP server processes import from here, so a tool running inside an MCP
server gets the same connection semantics as a request handler -- one session per
unit of work, committed by the caller, always closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.exceptions import DatabaseError
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """Build engine options appropriate to the configured backend.

    SQLite's async driver does not accept the QueuePool sizing arguments that
    PostgreSQL uses, so they are only applied when they are meaningful.
    """
    kwargs: dict[str, Any] = {"echo": settings.database_echo, "future": True, "pool_pre_ping": True}
    if not settings.uses_sqlite:
        kwargs |= {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_recycle": 1800,
        }
    return kwargs


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Apply SQLite pragmas needed for multi-connection use.

    The API process and each MCP server process open their own connections to the
    same database. On PostgreSQL that is unremarkable; on SQLite the defaults
    (rollback journal, no busy timeout) mean a second writer fails immediately with
    ``database is locked``.

    * ``journal_mode=WAL`` lets readers proceed while a writer is active.
    * ``busy_timeout`` makes a blocked writer wait rather than fail instantly.
    * ``foreign_keys=ON`` -- SQLite ignores foreign keys unless asked, so without
      this the test database would not enforce the constraints production does.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(settings.database_url, **_engine_kwargs(settings))
        if settings.uses_sqlite:
            _configure_sqlite(_engine)
        logger.info(
            "database engine created",
            extra={
                "event": "db.engine_created",
                "backend": settings.database_url.split("://", 1)[0],
            },
        )
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings),
            class_=AsyncSession,
            expire_on_commit=False,  # keep attributes usable after commit in async code
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Transactional scope around a unit of work.

    Commits on success, rolls back on failure, and translates driver errors into
    :class:`~app.core.exceptions.DatabaseError` so no SQL detail escapes to a client.
    """
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.exception("database transaction failed", extra={"event": "db.transaction_failed"})
        raise DatabaseError("A database operation failed.") from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with session_scope() as session:
        yield session


async def create_all(settings: Settings | None = None) -> None:
    """Create the schema from ORM metadata.

    Convenient for local development, tests and the containerised demo. Production
    deployments should run Alembic migrations instead -- see ``alembic/README.md``.
    """
    from app.models.database import Base

    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("database schema ensured", extra={"event": "db.schema_ensured"})


async def dispose_engine() -> None:
    """Dispose the engine and reset module state (used on shutdown and in tests)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def configure_engine(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Inject a pre-built engine/factory. Used by the test harness."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = session_factory
