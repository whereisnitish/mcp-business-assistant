"""Approval workflow tests.

The properties asserted here are the ones that stop an agent from performing an
irreversible action nobody sanctioned: an approval is bound to specific arguments,
can be used once, expires, and can only be decided by someone entitled to decide it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalStateError,
    AuthorizationError,
    NotFoundError,
)
from app.core.security import hash_payload
from app.models.database.conversation import Conversation
from app.models.database.enums import ApprovalStatus, PermissionLevel, UserRole
from app.models.database.user import User
from app.repositories.conversation_repository import ConversationRepository
from app.security.policy import PolicyDecision, PolicyOutcome
from app.services.approval_service import ApprovalGrant, ApprovalService
from tests.fixtures.factories import SEND_EMAIL_ARGS

pytestmark = pytest.mark.unit


def send_email_decision() -> PolicyDecision:
    return PolicyDecision(
        outcome=PolicyOutcome.REQUIRE_APPROVAL,
        permission=PermissionLevel.HIGH_RISK,
        tool_name="email__send_email",
        reason="High risk.",
        summary="Send an email with the subject 'Following up' to 1 recipient(s): ada@example.com.",
        risk_reason="Email is irreversible.",
    )


@pytest.fixture
async def conversation(session: AsyncSession, user: User) -> Conversation:
    created = await ConversationRepository(session).create_conversation(
        user_id=user.id, title="test"
    )
    await session.commit()
    return created


@pytest.fixture
def approvals(session: AsyncSession, settings: Settings) -> ApprovalService:
    return ApprovalService(session, settings=settings)


async def make_request(
    approvals: ApprovalService,
    conversation: Conversation,
    user: User,
    arguments: dict | None = None,
):  # type: ignore[no-untyped-def]
    return await approvals.create_request(
        conversation_id=conversation.id,
        user_id=user.id,
        decision=send_email_decision(),
        server_name="email",
        arguments=arguments if arguments is not None else dict(SEND_EMAIL_ARGS),
    )


class TestCreateApproval:
    async def test_request_is_pending_and_hash_bound(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        request = await make_request(approvals, conversation, user)

        assert request.status is ApprovalStatus.PENDING
        assert request.arguments_hash == hash_payload(SEND_EMAIL_ARGS)
        assert request.verify_integrity()
        assert request.is_actionable

    async def test_summary_comes_from_the_policy_engine_not_the_model(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        """What the approver reads must be derived from the actual arguments."""
        request = await make_request(approvals, conversation, user)
        assert "ada@example.com" in request.summary

    async def test_expiry_is_set_from_configuration(
        self, approvals: ApprovalService, conversation: Conversation, user: User, settings: Settings
    ) -> None:
        request = await make_request(approvals, conversation, user)
        expected = datetime.now(UTC) + timedelta(minutes=settings.approval_ttl_minutes)
        assert abs((request.expires_at - expected).total_seconds()) < 30


class TestConfirm:
    async def test_confirming_approves_and_returns_a_matching_grant(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        request = await make_request(approvals, conversation, user)

        confirmed, grant = await approvals.confirm(request.id, approver=user)

        assert confirmed.status is ApprovalStatus.APPROVED
        assert confirmed.decided_by_id == user.id
        assert grant.matches("email__send_email", SEND_EMAIL_ARGS)

    async def test_confirmation_is_single_use(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        """A replayed confirmation must not produce a second execution."""
        request = await make_request(approvals, conversation, user)
        await approvals.confirm(request.id, approver=user)

        with pytest.raises(ApprovalStateError):
            await approvals.confirm(request.id, approver=user)

    async def test_expired_requests_cannot_be_confirmed(
        self,
        approvals: ApprovalService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
    ) -> None:
        request = await make_request(approvals, conversation, user)
        request.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

        with pytest.raises(ApprovalExpiredError):
            await approvals.confirm(request.id, approver=user)

        await session.refresh(request)
        assert request.status is ApprovalStatus.EXPIRED

    async def test_tampered_arguments_are_refused(
        self,
        approvals: ApprovalService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
    ) -> None:
        """The central guarantee: approved payload == executed payload.

        Simulates the arguments being altered after a human saw them. The digest no
        longer matches, so the request is failed rather than approved.
        """
        request = await make_request(approvals, conversation, user)
        request.arguments = {**SEND_EMAIL_ARGS, "to": ["attacker@evil.example"]}
        await session.commit()

        with pytest.raises(ApprovalIntegrityError):
            await approvals.confirm(request.id, approver=user)

        await session.refresh(request)
        assert request.status is ApprovalStatus.FAILED

    async def test_a_viewer_cannot_approve(
        self, approvals: ApprovalService, conversation: Conversation, user: User, viewer: User
    ) -> None:
        request = await make_request(approvals, conversation, user)
        with pytest.raises(AuthorizationError):
            await approvals.confirm(request.id, approver=viewer)

    async def test_another_operator_cannot_approve_your_action(
        self,
        approvals: ApprovalService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
    ) -> None:
        """Otherwise any user could rubber-stamp anyone else's pending email."""
        from app.repositories.user_repository import UserRepository

        other = await UserRepository(session).create_user(
            email="other@example.com", role=UserRole.OPERATOR
        )
        await session.commit()

        request = await make_request(approvals, conversation, user)
        with pytest.raises(AuthorizationError):
            await approvals.confirm(request.id, approver=other)

    async def test_an_admin_may_approve_another_users_action(
        self, approvals: ApprovalService, conversation: Conversation, user: User, admin: User
    ) -> None:
        request = await make_request(approvals, conversation, user)
        confirmed, _ = await approvals.confirm(request.id, approver=admin)
        assert confirmed.status is ApprovalStatus.APPROVED

    async def test_missing_approval_raises_not_found(self, approvals: ApprovalService) -> None:
        with pytest.raises(NotFoundError):
            await approvals.get(uuid.uuid4())


class TestReject:
    async def test_rejecting_marks_the_request_and_prevents_confirmation(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        request = await make_request(approvals, conversation, user)

        rejected = await approvals.reject(request.id, approver=user, note="wrong recipients")

        assert rejected.status is ApprovalStatus.REJECTED
        assert rejected.decision_note == "wrong recipients"
        with pytest.raises(ApprovalStateError):
            await approvals.confirm(request.id, approver=user)


class TestGrantVerification:
    async def test_a_forged_grant_is_rejected(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        """A grant object carries no authority of its own.

        Anyone can construct the dataclass; verification reads the database, so a
        fabricated grant for a non-existent approval fails.
        """
        forged = ApprovalGrant(
            approval_id=uuid.uuid4(),
            tool_name="email__send_email",
            server_name="email",
            arguments=dict(SEND_EMAIL_ARGS),
            arguments_hash=hash_payload(SEND_EMAIL_ARGS),
            approved_by=user.id,
            approved_at=datetime.now(UTC),
        )
        with pytest.raises(NotFoundError):
            await approvals.verify_grant(forged, tool_name="email__send_email")

    async def test_a_grant_for_a_pending_request_is_rejected(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        """Minting a grant without going through confirm() must not work."""
        request = await make_request(approvals, conversation, user)
        premature = ApprovalGrant(
            approval_id=request.id,
            tool_name=request.tool_name,
            server_name=request.server_name,
            arguments=dict(request.arguments),
            arguments_hash=request.arguments_hash,
            approved_by=user.id,
            approved_at=datetime.now(UTC),
        )
        with pytest.raises(ApprovalStateError):
            await approvals.verify_grant(premature, tool_name=request.tool_name)

    async def test_a_grant_cannot_be_reused_for_a_different_tool(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        request = await make_request(approvals, conversation, user)
        _, grant = await approvals.confirm(request.id, approver=user)

        with pytest.raises(ApprovalIntegrityError):
            await approvals.verify_grant(grant, tool_name="crm__create_lead")

    async def test_a_grant_with_swapped_arguments_is_rejected(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        """The attack this whole mechanism exists to stop."""
        request = await make_request(approvals, conversation, user)
        _, grant = await approvals.confirm(request.id, approver=user)

        swapped = ApprovalGrant(
            approval_id=grant.approval_id,
            tool_name=grant.tool_name,
            server_name=grant.server_name,
            arguments={**SEND_EMAIL_ARGS, "to": ["attacker@evil.example"]},
            arguments_hash=grant.arguments_hash,
            approved_by=grant.approved_by,
            approved_at=grant.approved_at,
        )
        with pytest.raises(ApprovalIntegrityError):
            await approvals.verify_grant(swapped, tool_name=grant.tool_name)


class TestExpiry:
    async def test_stale_requests_are_expired_in_bulk(
        self,
        approvals: ApprovalService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
    ) -> None:
        request = await make_request(approvals, conversation, user)
        request.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

        assert await approvals.expire_stale() == 1

        await session.refresh(request)
        assert request.status is ApprovalStatus.EXPIRED

    async def test_live_requests_are_untouched(
        self, approvals: ApprovalService, conversation: Conversation, user: User
    ) -> None:
        request = await make_request(approvals, conversation, user)
        assert await approvals.expire_stale() == 0
        assert request.status is ApprovalStatus.PENDING
