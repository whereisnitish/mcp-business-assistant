"""Declarative base, naming conventions and shared column mixins.

Column types are chosen to be portable across PostgreSQL (production) and SQLite
(tests): :class:`sqlalchemy.Uuid` renders as a native ``UUID`` on PostgreSQL and a
``CHAR(32)`` elsewhere, and :class:`sqlalchemy.JSON` maps to ``JSONB``-compatible
``JSON`` on PostgreSQL and a serialised ``TEXT`` on SQLite. That portability is what
lets the integration suite run the real MCP servers against a real database without
requiring a PostgreSQL instance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import UTCDateTime

#: Deterministic constraint names, so Alembic autogenerate produces stable
#: migrations instead of database-assigned identifiers.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used as the Python-side default for timestamps."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    """Adds a client-generatable UUID primary key.

    UUIDs (rather than sequences) mean the application can mint an id before the
    row is flushed -- useful for correlating an audit log entry with the approval
    request it describes inside the same transaction.
    """

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
