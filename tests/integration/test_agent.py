"""Agent workflow tests.

A scripted LLM makes the agent deterministic, so these assert on behaviour that
would otherwise be untestable: that discovered tools are actually offered to the
model, that multi-step sequences chain correctly, that a high-risk step halts the
run, and that failures are fed back rather than crashing it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import AgentRunner
from app.agents.state import AgentStatus
from app.core.config import Settings
from app.mcp.manager import MCPManager
from app.models.database.conversation import Conversation
from app.models.database.enums import ApprovalStatus, ConversationStatus, MessageRole
from app.models.database.user import User
from app.providers.llm.fake_provider import (
    FakeLLMProvider,
    multi_tool_call_response,
    text_response,
    tool_call_response,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.security.policy import PolicyEngine
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.conversation_service import ConversationService
from app.services.tool_execution_service import ToolExecutionService

pytestmark = pytest.mark.integration


def build_runner(
    session: AsyncSession, manager: MCPManager, llm: FakeLLMProvider, settings: Settings
) -> AgentRunner:
    audit = AuditService(session)
    return AgentRunner(
        llm=llm,
        manager=manager,
        tools=ToolExecutionService(
            manager=manager,
            approvals=ApprovalService(session, audit, settings),
            audit=audit,
            policy=PolicyEngine(settings),
            settings=settings,
        ),
        conversations=ConversationService(session, settings),
        audit=audit,
        settings=settings,
    )


@pytest.fixture
async def conversation(session: AsyncSession, user: User) -> Conversation:
    created = await ConversationRepository(session).create_conversation(user_id=user.id)
    await session.commit()
    return created


class TestToolDiscovery:
    async def test_the_model_is_offered_the_discovered_tools(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        """Nothing hard-codes the tool list -- it comes from the servers at runtime."""
        llm = FakeLLMProvider([text_response("Hello.")])
        runner = build_runner(session, mcp_manager, llm, settings)

        await runner.run(user=user, conversation=conversation, message="hello")

        offered = llm.last_tools_offered()
        assert "crm__get_leads" in offered
        assert "email__send_email" in offered
        assert len(offered) == len(mcp_manager.catalog)

    async def test_a_viewer_is_only_offered_read_tools(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        viewer: User,
    ) -> None:
        conversation = await ConversationRepository(session).create_conversation(user_id=viewer.id)
        await session.commit()
        llm = FakeLLMProvider([text_response("I can only read.")])
        runner = build_runner(session, mcp_manager, llm, settings)

        await runner.run(user=viewer, conversation=conversation, message="add a lead")

        assert "crm__create_lead" not in llm.last_tools_offered()
        assert "crm__get_leads" in llm.last_tools_offered()


class TestSingleStep:
    async def test_a_read_request_calls_a_tool_and_answers(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        llm = FakeLLMProvider(
            [
                tool_call_response("crm__get_leads", {"status": "qualified"}),
                text_response("You have no qualified leads."),
            ]
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(
            user=user, conversation=conversation, message="show qualified leads"
        )

        assert result.status is AgentStatus.COMPLETED
        assert result.tools_called == ["crm__get_leads"]
        assert result.response == "You have no qualified leads."

    async def test_the_transcript_records_the_full_turn_structure(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        """Tool calls and results are persisted, not just prose."""
        llm = FakeLLMProvider(
            [tool_call_response("crm__get_leads", {}), text_response("Nothing found.")]
        )
        runner = build_runner(session, mcp_manager, llm, settings)
        await runner.run(user=user, conversation=conversation, message="leads?")

        messages = await ConversationRepository(session).list_messages(conversation.id)
        roles = [message.role for message in messages]
        assert roles == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]
        assert messages[1].tool_calls and messages[1].tool_calls[0]["name"] == "crm__get_leads"
        assert messages[2].tool_name == "crm__get_leads"


class TestMultiStep:
    async def test_the_agent_chains_tools_across_iterations(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        llm = FakeLLMProvider(
            [
                tool_call_response(
                    "crm__create_lead", {"full_name": "Ada", "email": "ada@example.com"}
                ),
                tool_call_response("tasks__create_task", {"title": "Follow up with Ada"}),
                text_response("Added Ada and created a follow-up task."),
            ]
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(
            user=user, conversation=conversation, message="add Ada and follow up"
        )

        assert result.tools_called == ["crm__create_lead", "tasks__create_task"]
        assert result.iterations == 3
        assert await LeadRepository(session).get_by_email("ada@example.com") is not None

    async def test_parallel_tool_calls_all_execute(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        llm = FakeLLMProvider(
            [
                multi_tool_call_response(
                    [("crm__get_leads", {}), ("tasks__get_overdue_tasks", {})]
                ),
                text_response("Here is both."),
            ]
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(
            user=user, conversation=conversation, message="leads and overdue tasks"
        )

        assert set(result.tools_called) == {"crm__get_leads", "tasks__get_overdue_tasks"}

    async def test_the_iteration_ceiling_stops_a_looping_model(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        """A model that never stops calling tools must not run forever."""
        looping = FakeLLMProvider(default=tool_call_response("crm__get_leads", {}))
        runner = build_runner(session, mcp_manager, looping, settings)

        result = await runner.run(user=user, conversation=conversation, message="loop")

        assert result.iterations <= settings.agent_max_iterations
        assert result.response


class TestApprovalPause:
    async def test_a_high_risk_step_halts_the_run(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        llm = FakeLLMProvider(
            [
                tool_call_response(
                    "email__send_email",
                    {"to": ["ada@example.com"], "subject": "Hi", "body": "Hello"},
                ),
                text_response("should not be reached"),
            ],
            default=text_response("done"),
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(user=user, conversation=conversation, message="email Ada")

        assert result.status is AgentStatus.AWAITING_APPROVAL
        assert result.approval_required
        assert result.pending_approval is not None
        assert result.pending_approval.tool_name == "email__send_email"
        assert "ada@example.com" in result.pending_approval.summary
        assert conversation.status is ConversationStatus.AWAITING_APPROVAL

    async def test_later_steps_do_not_run_after_a_pause(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        """A paused batch must not let subsequent actions slip through."""
        llm = FakeLLMProvider(
            [
                multi_tool_call_response(
                    [
                        (
                            "email__send_email",
                            {"to": ["a@example.com"], "subject": "s", "body": "b"},
                        ),
                        ("crm__create_lead", {"full_name": "Ghost", "email": "ghost@example.com"}),
                    ]
                )
            ],
            default=text_response("paused"),
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        await runner.run(user=user, conversation=conversation, message="send and add")

        assert await LeadRepository(session).get_by_email("ghost@example.com") is None

    async def test_confirming_executes_and_the_agent_reports_back(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        llm = FakeLLMProvider(
            [
                tool_call_response(
                    "email__send_email",
                    {"to": ["ada@example.com"], "subject": "Resumed", "body": "Hello"},
                )
            ],
            default=text_response("The email has been sent."),
        )
        runner = build_runner(session, mcp_manager, llm, settings)
        approvals = ApprovalService(session, AuditService(session), settings)

        first = await runner.run(user=user, conversation=conversation, message="email Ada")
        assert first.pending_approval is not None

        _, grant = await approvals.confirm(first.pending_approval.approval_id, approver=user)
        resumed = await runner.resume_after_approval(
            user=user, conversation=conversation, grant=grant
        )

        assert resumed.status is AgentStatus.COMPLETED
        approval = await approvals.get(first.pending_approval.approval_id)
        assert approval.status is ApprovalStatus.EXECUTED
        assert "Resumed" in (settings.email_outbox_dir / "outbox.jsonl").read_text(encoding="utf-8")


class TestFailureHandling:
    async def test_a_tool_failure_is_fed_back_to_the_model(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        """The model should get a chance to recover, not have the run aborted."""
        llm = FakeLLMProvider(
            [
                tool_call_response(
                    "crm__get_lead", {"lead_id": "00000000-0000-0000-0000-000000000000"}
                ),
                text_response("That lead does not exist."),
            ]
        )
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(user=user, conversation=conversation, message="get that lead")

        assert result.status is AgentStatus.COMPLETED
        assert result.errors
        second_call_messages = llm.calls[1][0]
        assert any(
            message.role.value == "tool" and "No lead exists" in (message.content or "")
            for message in second_call_messages
        )

    async def test_an_llm_outage_degrades_gracefully(
        self,
        session: AsyncSession,
        mcp_manager: MCPManager,
        settings: Settings,
        user: User,
        conversation: Conversation,
    ) -> None:
        from app.core.exceptions import LLMError

        llm = FakeLLMProvider(raise_on_call=LLMError("provider is down"))
        runner = build_runner(session, mcp_manager, llm, settings)

        result = await runner.run(user=user, conversation=conversation, message="anything")

        assert result.status is AgentStatus.FAILED
        assert "could not reach the language model" in result.response
