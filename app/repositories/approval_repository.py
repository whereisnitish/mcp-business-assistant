"""Approval request persistence.

The interesting method here is :meth:`ApprovalRepository.claim_pending`, which
performs the pending -> approved transition as a **conditional UPDATE**. Doing the
check and the write in one statement means two concurrent confirmations of the same
approval cannot both succeed: exactly one gets ``rowcount == 1``. A read-then-write
in Python would leave a race in which a high-risk action executes twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update

from app.models.database.approval import ApprovalRequest
from app.models.database.enums import ApprovalStatus, PermissionLevel
from app.repositories.base import BaseRepository, clamp_limit


class ApprovalRepository(BaseRepository[ApprovalRequest]):
    model = ApprovalRequest

    async def create_request(
        self,
        *,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        tool_name: str,
        server_name: str,
        arguments: dict[str, Any],
        arguments_hash: str,
        permission_level: PermissionLevel,
        summary: str,
        risk_reason: str,
        expires_at: datetime,
        tool_call_id: str | None = None,
    ) -> ApprovalRequest:
        return await self.add(
            ApprovalRequest(
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name=tool_name,
                server_name=server_name,
                arguments=arguments,
                arguments_hash=arguments_hash,
                permission_level=permission_level,
                summary=summary,
                risk_reason=risk_reason,
                expires_at=expires_at,
                tool_call_id=tool_call_id,
                status=ApprovalStatus.PENDING,
            )
        )

    async def list_pending(
        self,
        *,
        user_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> Sequence[ApprovalRequest]:
        stmt = select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.PENDING)
        if user_id is not None:
            stmt = stmt.where(ApprovalRequest.user_id == user_id)
        if conversation_id is not None:
            stmt = stmt.where(ApprovalRequest.conversation_id == conversation_id)
        stmt = stmt.order_by(ApprovalRequest.created_at.desc()).limit(clamp_limit(limit))
        return (await self.session.scalars(stmt)).all()

    async def claim_pending(
        self,
        approval_id: uuid.UUID,
        *,
        decided_by_id: uuid.UUID,
        note: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Atomically move PENDING -> APPROVED for a still-valid request.

        Returns ``True`` only for the caller that won the race. The ``expires_at``
        predicate is evaluated by the database, closing the window between an
        expiry check in Python and the write that follows it.
        """
        moment = now or datetime.now(UTC)
        result = await self.session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.expires_at > moment,
            )
            .values(
                status=ApprovalStatus.APPROVED,
                decided_at=moment,
                decided_by_id=decided_by_id,
                decision_note=note,
            )
            # The ORM would otherwise re-evaluate this WHERE clause in Python against
            # objects already in the session. We refresh explicitly after the write,
            # so that work is both wasted and a source of type-comparison errors.
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        # DML through `execute` returns a CursorResult, which is what exposes rowcount.
        return bool(cast(CursorResult[Any], result).rowcount)

    async def reject(
        self,
        approval_id: uuid.UUID,
        *,
        decided_by_id: uuid.UUID,
        note: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Atomically move PENDING -> REJECTED."""
        moment = now or datetime.now(UTC)
        result = await self.session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id, ApprovalRequest.status == ApprovalStatus.PENDING
            )
            .values(
                status=ApprovalStatus.REJECTED,
                decided_at=moment,
                decided_by_id=decided_by_id,
                decision_note=note,
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        # DML through `execute` returns a CursorResult, which is what exposes rowcount.
        return bool(cast(CursorResult[Any], result).rowcount)

    async def mark_executed(
        self,
        approval: ApprovalRequest,
        *,
        result: dict[str, Any] | None,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        approval.status = ApprovalStatus.EXECUTED
        approval.executed_at = now or datetime.now(UTC)
        approval.execution_result = result
        await self.session.flush()
        return approval

    async def mark_failed(
        self, approval: ApprovalRequest, *, error: str, now: datetime | None = None
    ) -> ApprovalRequest:
        approval.status = ApprovalStatus.FAILED
        approval.executed_at = now or datetime.now(UTC)
        approval.error = error
        await self.session.flush()
        return approval

    async def expire_stale(self, *, now: datetime | None = None) -> int:
        """Bulk-expire every pending request past its TTL. Returns the row count."""
        moment = now or datetime.now(UTC)
        result = await self.session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.expires_at <= moment,
            )
            .values(status=ApprovalStatus.EXPIRED, decided_at=moment)
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return int(cast(CursorResult[Any], result).rowcount or 0)
