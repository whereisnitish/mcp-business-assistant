"""Agent runner: assembles dependencies, runs the graph, returns a result.

The seam between the API layer and the agent. Route handlers construct a runner and
call :meth:`AgentRunner.run`; they never touch LangGraph, the MCP manager or the
LLM provider directly.

It also owns the **resume-after-approval** path, which is what turns approval from a
refusal into a workflow: once a human confirms, the stored action executes and the
agent writes the closing message, in the same conversation, with the same history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agents.graph import build_agent_graph
from app.agents.nodes import AgentDependencies
from app.agents.prompts import build_system_prompt
from app.agents.state import AgentState, AgentStatus, PendingApproval
from app.core.config import Settings, get_settings
from app.core.exceptions import AgentError, AppError
from app.core.logging import Timer, get_logger, safe_extra
from app.mcp.manager import MCPManager
from app.models.database.conversation import Conversation
from app.models.database.enums import ConversationStatus
from app.models.database.user import User
from app.providers.llm.base import LLMMessage, LLMProvider
from app.security.policy import PolicyContext
from app.services.approval_service import ApprovalGrant
from app.services.audit_service import AuditService
from app.services.conversation_service import ConversationService
from app.services.tool_execution_service import ToolExecutionService

logger = get_logger(__name__)


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of one agent run, as the API layer needs it."""

    conversation_id: str
    response: str
    status: AgentStatus
    pending_approval: PendingApproval | None = None
    tools_called: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    iterations: int = 0
    duration_ms: float = 0.0

    @property
    def approval_required(self) -> bool:
        return self.pending_approval is not None


class AgentRunner:
    """Runs the agent workflow for one request."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        manager: MCPManager,
        tools: ToolExecutionService,
        conversations: ConversationService,
        audit: AuditService,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._manager = manager
        self._tools = tools
        self._conversations = conversations
        self._audit = audit
        self._settings = settings or get_settings()

    # --------------------------------------------------------------------- run --
    async def run(
        self, *, user: User, conversation: Conversation, message: str, request_id: str | None = None
    ) -> AgentRunResult:
        """Process one user message end to end."""
        timer = Timer()
        await self._conversations.add_user_message(conversation, message)

        state = AgentState(
            conversation_id=str(conversation.id),
            user_id=str(user.id),
            user_role=user.role,
            request_id=request_id,
            current_request=message,
            max_iterations=self._settings.agent_max_iterations,
        )
        deps = self._build_dependencies(conversation)

        try:
            raw = await build_agent_graph(deps).ainvoke(state)
        except AppError:
            raise
        except Exception as exc:
            # An unexpected failure inside the graph must not leak internals to the
            # caller; the traceback goes to the log, a generic message to the user.
            logger.exception(
                "agent run failed unexpectedly",
                extra=safe_extra({"event": "agent.run_failed", "error_type": type(exc).__name__}),
            )
            await self._audit.agent_error(
                error=f"{type(exc).__name__}", conversation_id=conversation.id, user_id=user.id
            )
            raise AgentError("The assistant failed to process that request.") from exc

        final = AgentState.model_validate(raw) if isinstance(raw, dict) else raw
        await self._sync_conversation_status(conversation, final)

        return AgentRunResult(
            conversation_id=str(conversation.id),
            response=final.final_response or "I was not able to produce a response.",
            status=final.status,
            pending_approval=final.pending_approval,
            tools_called=final.selected_tools,
            errors=final.errors,
            iterations=final.iteration,
            duration_ms=timer.elapsed_ms,
        )

    # ------------------------------------------------------------------ resume --
    async def resume_after_approval(
        self, *, user: User, conversation: Conversation, grant: ApprovalGrant
    ) -> AgentRunResult:
        """Execute an approved action and write the closing message.

        The arguments come from the approval record, never from the confirming
        request, so confirming cannot alter what runs.
        """
        timer = Timer()
        context = PolicyContext(
            user_id=str(user.id), user_role=user.role, conversation_id=str(conversation.id)
        )

        outcome = await self._tools.execute_approved(
            grant, context, conversation_id=conversation.id, user_id=user.id
        )
        result = outcome.agent_visible_result()
        await self._conversations.add_tool_result(conversation, result)

        response = await self._compose_completion(
            conversation, user, grant, outcome_success=result.success
        )
        await self._conversations.add_assistant_message(
            conversation,
            content=response,
            extra_metadata={"approval_id": str(grant.approval_id), "executed": result.success},
        )
        await self._conversations.set_status(conversation, ConversationStatus.ACTIVE)

        return AgentRunResult(
            conversation_id=str(conversation.id),
            response=response,
            status=AgentStatus.COMPLETED if result.success else AgentStatus.FAILED,
            tools_called=[grant.tool_name],
            errors=[] if result.success else [result.error or "The approved action failed."],
            duration_ms=timer.elapsed_ms,
        )

    async def _compose_completion(
        self, conversation: Conversation, user: User, grant: ApprovalGrant, *, outcome_success: bool
    ) -> str:
        """Ask the model to summarise the executed action.

        The history ends with the approved tool's result, which is the standard cue
        for a chat model to produce a closing answer -- no synthetic "now summarise"
        turn is injected. That matters beyond tidiness: an extra user message would
        become the newest turn, and any component that scopes tool results to the
        current turn would then see none of the results it is meant to describe.

        Tools are deliberately not offered here. The approved action has already run
        and the only remaining job is to report it; offering tools would let a second
        action be proposed from inside a confirmation request.
        """
        history = await self._conversations.load_history(conversation)
        messages = [LLMMessage.system(build_system_prompt()), *history]
        try:
            response = await self._llm.complete(messages, tools=None)
        except AppError:
            response = None  # fall through to the deterministic message below

        if response is not None and response.content:
            return response.content
        return (
            f"Done -- {grant.tool_name} has been executed."
            if outcome_success
            else f"The approved action {grant.tool_name} could not be completed."
        )

    # ----------------------------------------------------------------- helpers --
    def _build_dependencies(self, conversation: Conversation) -> AgentDependencies:
        return AgentDependencies(
            llm=self._llm,
            manager=self._manager,
            tools=self._tools,
            conversations=self._conversations,
            audit=self._audit,
            conversation=conversation,
            settings=self._settings,
        )

    async def _sync_conversation_status(
        self, conversation: Conversation, state: AgentState
    ) -> None:
        """Mirror the run's outcome onto the conversation row."""
        status = (
            ConversationStatus.AWAITING_APPROVAL
            if state.status == AgentStatus.AWAITING_APPROVAL
            else ConversationStatus.ACTIVE
        )
        if conversation.status != status:
            await self._conversations.set_status(conversation, status)


def build_pending_approval_payload(pending: PendingApproval | None) -> dict[str, Any] | None:
    """Serialise a pending approval for an API response."""
    return pending.model_dump(mode="json") if pending else None


def parse_uuid(value: str) -> uuid.UUID:
    """Parse a UUID, raising a client-safe error for malformed input."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AgentError(f"{value!r} is not a valid identifier.") from exc
