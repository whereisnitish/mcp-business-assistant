"""Portable column types.

These live in the ``db`` package rather than alongside the models because
``app.db.base`` itself needs them: putting them under ``app.models.database`` made
the declarative base depend on the model package that imports it.

``JSONColumn`` is ``JSONB`` on PostgreSQL -- which gives indexing and containment
operators for querying audit payloads -- and falls back to the generic ``JSON``
type everywhere else, so the same models work under SQLite in the test suite.

``MoneyColumn`` uses ``Numeric`` rather than ``Float``: binary floating point cannot
represent decimal currency exactly, and a sales report that silently loses cents is
worse than one that fails loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect

#: JSON payloads (audit context, tool arguments, structured results).
JSONColumn = JSON().with_variant(postgresql.JSONB(), "postgresql")

#: Monetary amounts: 14 integral digits, 2 decimal places.
MoneyColumn = Numeric(16, 2)


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL's ``TIMESTAMPTZ`` round-trips an aware datetime; SQLite has no
    timezone concept and hands back a **naive** one. That difference is subtle and
    nasty: code that works in production raises
    ``can't compare offset-naive and offset-aware datetimes`` under the test suite,
    or -- worse -- silently compares wrong values.

    This decorator normalises both directions, so every datetime read from any
    backend is aware UTC and the test database behaves like the production one.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """On write: treat a naive value as UTC, and convert any aware value to UTC."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """On read: attach UTC when the backend returned a naive value."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
