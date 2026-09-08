"""Generic repository base.

Repositories own *all* SQL. Nothing above this layer -- not the MCP tools, not the
services, not the agent -- constructs a query. That is what makes it possible to
swap the local PostgreSQL implementation of a business interface for a real SaaS
integration without touching the tool contracts.

Sessions are injected, never created here: the caller decides the transaction
boundary, so several repository operations can participate in one unit of work.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

#: Hard ceiling on any list query, applied even when a caller (or a model-proposed
#: tool argument) asks for more. Prevents an agent from pulling an entire table
#: into a prompt.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def clamp_limit(limit: int | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Clamp a caller-supplied limit into ``[1, MAX_PAGE_SIZE]``."""
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_PAGE_SIZE))


class BaseRepository[ModelT: Base]:
    """Common persistence operations for a single ORM model."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads --
    async def get(self, entity_id: uuid.UUID | str) -> ModelT | None:
        """Fetch by primary key, tolerating a string UUID from a tool argument."""
        identifier = self._coerce_uuid(entity_id)
        if identifier is None:
            return None
        return await self.session.get(self.model, identifier)

    async def list_all(self, *, limit: int | None = None, offset: int = 0) -> Sequence[ModelT]:
        stmt = select(self.model).limit(clamp_limit(limit)).offset(max(0, offset))
        return (await self.session.scalars(stmt)).all()

    async def count(self, stmt: Select[Any] | None = None) -> int:
        """Count rows, optionally for a pre-filtered statement.

        Counting over a subquery rather than rewriting the columns clause. The
        tempting ``stmt.with_only_columns(func.count())`` is wrong: replacing the
        columns also removes the FROM they implied, leaving a bare
        ``SELECT count(*)`` that returns **1** for an empty table. Wrapping the
        original statement preserves its FROM, joins, filters and DISTINCT.
        """
        base = stmt if stmt is not None else select(self.model)
        subquery = base.order_by(None).limit(None).offset(None).subquery()
        result = await self.session.execute(select(func.count()).select_from(subquery))
        return int(result.scalar_one())

    # ----------------------------------------------------------------- writes --
    async def add(self, entity: ModelT) -> ModelT:
        """Persist a new entity and flush so its generated columns are populated."""
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def delete_by_id(self, entity_id: uuid.UUID | str) -> int:
        identifier = self._coerce_uuid(entity_id)
        if identifier is None:
            return 0
        # `id` is declared on UUIDPrimaryKeyMixin, which the `Base` bound on ModelT
        # does not capture; every mapped model in this project carries it.
        primary_key = cast(Any, self.model).id
        result = await self.session.execute(delete(self.model).where(primary_key == identifier))
        await self.session.flush()
        # DML through `execute` returns a CursorResult, which is what exposes rowcount.
        return int(cast(CursorResult[Any], result).rowcount or 0)

    # ---------------------------------------------------------------- helpers --
    @staticmethod
    def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
        """Convert a tool-supplied identifier into a UUID.

        Returns ``None`` for anything malformed rather than raising: an LLM passing
        a nonsense id should produce a clean "not found", not a 500.
        """
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            return None
