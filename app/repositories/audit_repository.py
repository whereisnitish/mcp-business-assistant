"""Audit log persistence (append-only)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from app.models.database.audit import AuditLog
from app.models.database.enums import AuditEventType, AuditOutcome, PermissionLevel
from app.repositories.base import BaseRepository, clamp_limit


class AuditRepository(BaseRepository[AuditLog]):
    """Writes are append-only; there is deliberately no update or delete method."""

    model = AuditLog

    async def record(
        self,
        *,
        event_type: AuditEventType,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        request_id: str | None = None,
        conversation_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        approval_id: uuid.UUID | None = None,
        tool_name: str | None = None,
        server_name: str | None = None,
        permission_level: PermissionLevel | None = None,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            event_type=event_type,
            outcome=outcome,
            request_id=request_id,
            conversation_id=conversation_id,
            user_id=user_id,
            approval_id=approval_id,
            tool_name=tool_name,
            server_name=server_name,
            permission_level=permission_level,
            summary=summary,
            payload=payload,
            error=error,
            duration_ms=duration_ms,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.conversation_id == conversation_id)
            .order_by(AuditLog.created_at.asc())
            .limit(clamp_limit(limit, default=100))
        )
        return (await self.session.scalars(stmt)).all()

    async def list_for_request(
        self, request_id: str, *, limit: int | None = None
    ) -> Sequence[AuditLog]:
        """Reconstruct everything that happened while serving one HTTP request."""
        stmt = (
            select(AuditLog)
            .where(AuditLog.request_id == request_id)
            .order_by(AuditLog.created_at.asc())
            .limit(clamp_limit(limit, default=100))
        )
        return (await self.session.scalars(stmt)).all()

    async def list_by_event(
        self, event_type: AuditEventType, *, limit: int | None = None
    ) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.event_type == event_type)
            .order_by(AuditLog.created_at.desc())
            .limit(clamp_limit(limit))
        )
        return (await self.session.scalars(stmt)).all()
