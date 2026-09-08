"""Tool execution gateway tests, against real MCP servers.

Runs the genuine servers over the SDK's in-memory transport, so these exercise the
whole path -- policy check, argument validation, MCP dispatch, audit -- rather than a
mock of it. That matters here more than elsewhere: this service is the only route
from the agent to a real side effect.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall
from app.models.database.conversation import Conversation
from app.models.database.enums import ApprovalStatus, AuditEventType, UserRole
from app.models.database.user import User
from app.repositories.audit_repository import AuditRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.security.policy import PolicyEngine
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.tool_execution_service import ExecutionStatus, ToolExecutionService
from tests.fixtures.factories import make_policy_context

pytestmark = pytest.mark.integration


@pytest.fixture
async def conversation(session: AsyncSession, user: User) -> Conversation:
    created = await ConversationRepository(session).create_conversation(user_id=user.id)
    await session.commit()
    return created


@pytest.fixture
def executor(
    session: AsyncSession, mcp_manager: MCPManager, settings: Settings
) -> ToolExecutionService:
    audit = AuditService(session)
    return ToolExecutionService(
        manager=mcp_manager,
        approvals=ApprovalService(session, audit, settings),
        audit=audit,
        policy=PolicyEngine(settings),
        settings=settings,
    )


def call(tool: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call_{tool}", tool_name=tool, arguments=dict(arguments))


class TestReadTools:
    async def test_read_tool_executes_immediately(
        self, executor: ToolExecutionService, conversation: Conversation, user: User
    ) -> None:
        outcome = await executor.execute(
            call("crm__get_leads", status="qualified"),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.EXECUTED
        assert outcome.result is not None
        assert outcome.result.structured_content == {
            "leads": [],
            "count": 0,
            "filters_applied": {"status": "qualified"},
        }


class TestWriteTools:
    async def test_write_tool_persists_and_is_audited(
        self,
        executor: ToolExecutionService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
    ) -> None:
        outcome = await executor.execute(
            call("crm__create_lead", full_name="Ada Lovelace", email="ada@example.com"),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.EXECUTED

        lead = await LeadRepository(session).get_by_email("ada@example.com")
        assert lead is not None and lead.full_name == "Ada Lovelace"

        events = [entry.event_type for entry in await AuditRepository(session).list_all(limit=50)]
        assert AuditEventType.TOOL_CALL_STARTED in events
        assert AuditEventType.TOOL_CALL_SUCCEEDED in events


class TestHighRiskTools:
    async def test_send_email_does_not_execute_without_approval(
        self,
        executor: ToolExecutionService,
        conversation: Conversation,
        user: User,
        settings: Settings,
    ) -> None:
        """The headline guarantee: no email leaves without a human decision."""
        outcome = await executor.execute(
            call(
                "email__send_email",
                to=["ada@example.com"],
                subject="Never sent without approval",
                body="Hello",
            ),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )

        assert outcome.status is ExecutionStatus.APPROVAL_REQUIRED
        assert outcome.approval is not None
        assert outcome.approval.status is ApprovalStatus.PENDING

        outbox = settings.email_outbox_dir / "outbox.jsonl"
        assert not outbox.exists() or "Never sent without approval" not in outbox.read_text(
            encoding="utf-8"
        )

    async def test_approved_action_executes_the_stored_arguments(
        self,
        executor: ToolExecutionService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        approvals = ApprovalService(session, AuditService(session), settings)
        pending = await executor.execute(
            call(
                "email__send_email",
                to=["ada@example.com"],
                subject="Approved subject",
                body="Hello",
            ),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert pending.approval is not None

        _, grant = await approvals.confirm(pending.approval.id, approver=user)
        outcome = await executor.execute_approved(
            grant,
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )

        assert outcome.status is ExecutionStatus.EXECUTED
        outbox = (settings.email_outbox_dir / "outbox.jsonl").read_text(encoding="utf-8")
        assert "Approved subject" in outbox

    async def test_execution_is_refused_when_policy_would_now_deny(
        self,
        executor: ToolExecutionService,
        conversation: Conversation,
        user: User,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """A grant does not bypass the policy check.

        The approval is valid, but the caller is now acting as a viewer. Holding an
        approval must not let them execute what their role forbids.
        """
        approvals = ApprovalService(session, AuditService(session), settings)
        pending = await executor.execute(
            call("email__send_email", to=["ada@example.com"], subject="Hi", body="Hello"),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert pending.approval is not None
        _, grant = await approvals.confirm(pending.approval.id, approver=user)

        outcome = await executor.execute_approved(
            grant,
            make_policy_context(user_id=str(user.id), role=UserRole.VIEWER),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.DENIED


class TestRejection:
    async def test_unknown_tool_is_refused_before_dispatch(
        self, executor: ToolExecutionService, conversation: Conversation, user: User
    ) -> None:
        """A hallucinated tool name must never reach a server."""
        outcome = await executor.execute(
            call("crm__delete_everything"),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.DENIED
        assert "Unknown tool" in outcome.message

    async def test_invalid_arguments_are_rejected_with_a_usable_message(
        self, executor: ToolExecutionService, conversation: Conversation, user: User
    ) -> None:
        outcome = await executor.execute(
            call("crm__create_lead", email="ada@example.com"),  # missing full_name
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.DENIED
        assert "full_name" in outcome.message

    async def test_a_viewer_is_denied_a_write(
        self,
        executor: ToolExecutionService,
        conversation: Conversation,
        viewer: User,
        session: AsyncSession,
    ) -> None:
        outcome = await executor.execute(
            call("crm__create_lead", full_name="X", email="x@example.com"),
            make_policy_context(user_id=str(viewer.id), role=UserRole.VIEWER),
            conversation_id=conversation.id,
            user_id=viewer.id,
        )
        assert outcome.status is ExecutionStatus.DENIED
        assert await LeadRepository(session).get_by_email("x@example.com") is None

    async def test_a_failing_tool_returns_a_result_the_agent_can_read(
        self, executor: ToolExecutionService, conversation: Conversation, user: User
    ) -> None:
        """Tool failure is data, not an exception: the model must be able to react."""
        outcome = await executor.execute(
            call("crm__get_lead", lead_id="00000000-0000-0000-0000-000000000000"),
            make_policy_context(user_id=str(user.id)),
            conversation_id=conversation.id,
            user_id=user.id,
        )
        assert outcome.status is ExecutionStatus.FAILED
        assert outcome.result is not None and not outcome.result.success
        assert "No lead exists" in (outcome.result.error or "")
