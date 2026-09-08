"""Approval service -- the human-in-the-loop gate.

This is the component the rest of the security model rests on, so its guarantees
are worth stating precisely.

**An approval is bound to exact arguments.** The request stores the arguments a
human was shown plus their SHA-256 digest. Execution re-derives the digest and
refuses on mismatch, so the payload approved is provably the payload executed. The
model cannot draft a benign email, obtain approval, and then substitute recipients.

**Confirmation is atomic and single-use.** The PENDING -> APPROVED transition is a
conditional ``UPDATE ... WHERE status = 'pending' AND expires_at > now``. Exactly one
concurrent confirmation wins; a replayed confirmation finds no pending row and is
refused. Expiry is evaluated by the database, closing the check-then-act window.

**Only a human can approve.** :meth:`ApprovalService.confirm` is reachable solely
from the authenticated API endpoint. The agent has no path to it -- not through a
tool, not through the graph, not through prompt text.

**A grant is a claim, not proof.** :class:`ApprovalGrant` is an ordinary object, so
it deliberately does not carry authority on its own. The execution path re-reads the
row and re-verifies status, expiry, tool identity and argument hash against the
database. Authority lives in persisted state, never in an in-memory token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalStateError,
    AuthorizationError,
    NotFoundError,
)
from app.core.logging import get_logger, safe_extra
from app.core.security import hash_payload, verify_payload
from app.models.database.approval import ApprovalRequest
from app.models.database.enums import ApprovalStatus, AuditEventType, AuditOutcome, UserRole
from app.models.database.user import User
from app.repositories.approval_repository import ApprovalRepository
from app.security.policy import PolicyDecision
from app.services.audit_service import AuditService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Evidence that a human confirmed a specific action.

    Carries the *stored* arguments -- never arguments supplied at confirmation time
    -- so the executor runs what was approved rather than what a later caller asks
    for. Always re-verified against the database before use.
    """

    approval_id: uuid.UUID
    tool_name: str
    server_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    approved_by: uuid.UUID
    approved_at: datetime

    def matches(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Whether this grant authorises exactly this call."""
        return self.tool_name == tool_name and verify_payload(arguments, self.arguments_hash)


class ApprovalService:
    """Creates, decides and validates approval requests."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._repository = ApprovalRepository(session)
        self._audit = audit or AuditService(session)
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------ create --
    async def create_request(
        self,
        *,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        decision: PolicyDecision,
        server_name: str,
        arguments: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> ApprovalRequest:
        """Persist a proposed action for human review.

        The summary comes from the policy engine, which builds it from the actual
        arguments in backend code -- so what the approver reads is derived from what
        will run, not from model-authored prose.
        """
        expires_at = datetime.now(UTC) + timedelta(minutes=self._settings.approval_ttl_minutes)
        request = await self._repository.create_request(
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=decision.tool_name,
            server_name=server_name,
            arguments=arguments,
            arguments_hash=hash_payload(arguments),
            permission_level=decision.permission,
            summary=decision.summary,
            risk_reason=decision.risk_reason or decision.reason,
            expires_at=expires_at,
            tool_call_id=tool_call_id,
        )

        await self._audit.record(
            AuditEventType.APPROVAL_REQUESTED,
            outcome=AuditOutcome.PENDING,
            conversation_id=conversation_id,
            user_id=user_id,
            approval_id=request.id,
            tool_name=decision.tool_name,
            server_name=server_name,
            permission_level=decision.permission,
            summary=decision.summary,
            payload={"arguments": arguments, "expires_at": expires_at.isoformat()},
        )
        await self._session.commit()
        logger.info(
            "approval requested",
            extra=safe_extra(
                {
                    "event": "approval.requested",
                    "approval_id": str(request.id),
                    "tool": decision.tool_name,
                    "expires_at": expires_at.isoformat(),
                }
            ),
        )
        return request

    # -------------------------------------------------------------------- read --
    async def get(self, approval_id: uuid.UUID | str) -> ApprovalRequest:
        """Fetch an approval request or raise :class:`NotFoundError`."""
        request = await self._repository.get(approval_id)
        if request is None:
            raise NotFoundError(f"Approval request {approval_id} was not found.")
        return request

    async def list_pending(
        self, *, user: User, conversation_id: uuid.UUID | None = None, limit: int = 25
    ) -> list[ApprovalRequest]:
        """Pending approvals visible to *user*.

        Admins see everything; everyone else sees only their own, so one user cannot
        enumerate (or approve) actions proposed in another user's conversation.
        """
        scope = None if user.role == UserRole.ADMIN else user.id
        return list(
            await self._repository.list_pending(
                user_id=scope, conversation_id=conversation_id, limit=limit
            )
        )

    # ---------------------------------------------------------------- decisions --
    async def confirm(
        self, approval_id: uuid.UUID | str, *, approver: User, note: str | None = None
    ) -> tuple[ApprovalRequest, ApprovalGrant]:
        """Approve a pending request and mint a grant.

        Raises:
            NotFoundError: no such request.
            AuthorizationError: the approver may not decide this request.
            ApprovalExpiredError: the TTL elapsed before confirmation.
            ApprovalStateError: already decided, or lost the race to a concurrent confirm.
            ApprovalIntegrityError: the stored payload no longer matches its digest.
        """
        request = await self.get(approval_id)
        self._assert_can_decide(request, approver)

        # Integrity is checked before the transition: if the stored arguments have
        # been tampered with, the request must not become approvable at all.
        if not request.verify_integrity():
            await self._fail_integrity(request, approver)
            raise ApprovalIntegrityError()

        if request.status != ApprovalStatus.PENDING:
            raise ApprovalStateError(
                f"This approval request is already {request.status.value} and cannot be confirmed."
            )
        if request.is_expired():
            await self._expire(request)
            raise ApprovalExpiredError()

        claimed = await self._repository.claim_pending(
            request.id, decided_by_id=approver.id, note=note
        )
        if not claimed:
            # Another confirmation won, or the row expired between the check above
            # and this statement. Either way it is no longer ours to execute.
            await self._session.refresh(request)
            if request.is_expired():
                raise ApprovalExpiredError()
            raise ApprovalStateError(
                "This approval request was already decided by another request."
            )

        await self._session.refresh(request)
        await self._audit.record(
            AuditEventType.APPROVAL_CONFIRMED,
            conversation_id=request.conversation_id,
            user_id=approver.id,
            approval_id=request.id,
            tool_name=request.tool_name,
            server_name=request.server_name,
            permission_level=request.permission_level,
            summary=f"Approved by {approver.email}",
            payload={"note": note} if note else None,
        )

        await self._session.commit()
        grant = ApprovalGrant(
            approval_id=request.id,
            tool_name=request.tool_name,
            server_name=request.server_name,
            arguments=dict(request.arguments),
            arguments_hash=request.arguments_hash,
            approved_by=approver.id,
            approved_at=request.decided_at or datetime.now(UTC),
        )
        return request, grant

    async def reject(
        self, approval_id: uuid.UUID | str, *, approver: User, note: str | None = None
    ) -> ApprovalRequest:
        """Reject a pending request. The action is never executed."""
        request = await self.get(approval_id)
        self._assert_can_decide(request, approver)

        if request.status != ApprovalStatus.PENDING:
            raise ApprovalStateError(
                f"This approval request is already {request.status.value} and cannot be rejected."
            )

        if not await self._repository.reject(request.id, decided_by_id=approver.id, note=note):
            raise ApprovalStateError("This approval request was already decided.")

        await self._session.refresh(request)
        await self._audit.record(
            AuditEventType.APPROVAL_REJECTED,
            outcome=AuditOutcome.BLOCKED,
            conversation_id=request.conversation_id,
            user_id=approver.id,
            approval_id=request.id,
            tool_name=request.tool_name,
            permission_level=request.permission_level,
            summary=f"Rejected by {approver.email}",
            payload={"note": note} if note else None,
        )
        await self._session.commit()
        return request

    # --------------------------------------------------------------- execution --
    async def verify_grant(self, grant: ApprovalGrant, *, tool_name: str) -> ApprovalRequest:
        """Re-validate a grant against persisted state immediately before execution.

        This is the check that matters. Even a perfectly forged
        :class:`ApprovalGrant` fails here, because authority is read from the
        database row rather than taken from the object.
        """
        request = await self.get(grant.approval_id)

        if request.tool_name != tool_name or grant.tool_name != tool_name:
            raise ApprovalIntegrityError("The approval does not correspond to this tool.")
        if request.status != ApprovalStatus.APPROVED:
            raise ApprovalStateError(
                f"The approval is {request.status.value}; only an approved request can be executed."
            )
        if request.is_expired():
            await self._expire(request)
            raise ApprovalExpiredError()
        if not request.verify_integrity():
            await self._fail_integrity(request, None)
            raise ApprovalIntegrityError()
        if not verify_payload(grant.arguments, request.arguments_hash):
            raise ApprovalIntegrityError("The arguments do not match the approved payload.")
        return request

    async def mark_executed(
        self, request: ApprovalRequest, *, result: dict[str, Any] | None
    ) -> ApprovalRequest:
        updated = await self._repository.mark_executed(request, result=result)
        await self._session.commit()
        await self._audit.record(
            AuditEventType.APPROVAL_EXECUTED,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            approval_id=request.id,
            tool_name=request.tool_name,
            server_name=request.server_name,
            permission_level=request.permission_level,
            summary="Approved action executed",
        )
        return updated

    async def mark_failed(self, request: ApprovalRequest, *, error: str) -> ApprovalRequest:
        updated = await self._repository.mark_failed(request, error=error)
        await self._session.commit()
        await self._audit.record(
            AuditEventType.APPROVAL_EXECUTED,
            outcome=AuditOutcome.FAILURE,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            approval_id=request.id,
            tool_name=request.tool_name,
            error=error,
        )
        return updated

    async def expire_stale(self) -> int:
        """Expire every pending request past its TTL. Returns how many."""
        count = await self._repository.expire_stale()
        if count:
            await self._session.commit()
            logger.info(
                "expired stale approval requests",
                extra=safe_extra({"event": "approval.expired_batch", "count": count}),
            )
        return count

    # ----------------------------------------------------------------- helpers --
    def assert_can_view(self, request: ApprovalRequest, user: User) -> None:
        """Authorisation for *reading* an approval.

        Separate from the decision check because the rules differ: a viewer may
        inspect a request raised in their own conversation even though they may not
        decide it. Both checks are needed -- an approval payload can contain the
        full text and recipients of an unsent email.
        """
        if user.role != UserRole.ADMIN and request.user_id != user.id:
            raise AuthorizationError("You do not have access to this approval request.")

    def _assert_can_decide(self, request: ApprovalRequest, approver: User) -> None:
        """Authorisation for the decision itself.

        A viewer cannot approve anything; a non-admin can only decide requests
        raised in their own conversations. Without this, any authenticated user
        could approve another user's pending email.
        """
        if not approver.can_approve:
            raise AuthorizationError("Your role does not permit approving actions.")
        if approver.role != UserRole.ADMIN and request.user_id != approver.id:
            raise AuthorizationError("You can only decide approval requests that you raised.")

    async def _expire(self, request: ApprovalRequest) -> None:
        request.status = ApprovalStatus.EXPIRED
        request.decided_at = datetime.now(UTC)
        await self._session.flush()
        await self._audit.record(
            AuditEventType.APPROVAL_EXPIRED,
            outcome=AuditOutcome.BLOCKED,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            approval_id=request.id,
            tool_name=request.tool_name,
            summary="Approval request expired before it was confirmed",
        )

    async def _fail_integrity(self, request: ApprovalRequest, approver: User | None) -> None:
        """Record a tamper signal and take the request out of play."""
        request.status = ApprovalStatus.FAILED
        request.error = "argument integrity check failed"
        request.decided_at = datetime.now(UTC)
        await self._session.flush()
        logger.error(
            "approval payload failed its integrity check",
            extra=safe_extra(
                {
                    "event": "approval.integrity_failure",
                    "approval_id": str(request.id),
                    "tool": request.tool_name,
                }
            ),
        )
        await self._audit.record(
            AuditEventType.APPROVAL_EXECUTED,
            outcome=AuditOutcome.FAILURE,
            conversation_id=request.conversation_id,
            user_id=approver.id if approver else request.user_id,
            approval_id=request.id,
            tool_name=request.tool_name,
            error="The stored approval payload did not match its integrity hash.",
        )
