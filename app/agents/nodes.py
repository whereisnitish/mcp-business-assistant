"""Agent workflow nodes.

Each node is a small async function over :class:`AgentState` that returns the fields
it changed. They are deliberately free of framework specifics: no node imports
LangGraph, which keeps them independently testable -- a unit test calls
``plan(state, deps)`` directly, with no graph in sight.

Dependencies (the MCP manager, the LLM provider, the services) arrive in an
:class:`AgentDependencies` container rather than through state, because they are
collaborators, not data, and have no business being serialised alongside a
conversation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.agents.prompts import build_system_prompt
from app.agents.state import AgentState, AgentStatus, PendingApproval
from app.core.config import Settings
from app.core.exceptions import AppError, LLMError
from app.core.logging import get_logger, safe_extra
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall, ToolResult
from app.models.database.conversation import Conversation
from app.models.database.enums import AuditEventType, AuditOutcome, UserRole
from app.providers.llm.base import LLMMessage, LLMProvider
from app.security.policy import PolicyContext
from app.services.audit_service import AuditService
from app.services.conversation_service import ConversationService
from app.services.tool_execution_service import ToolExecutionService

logger = get_logger(__name__)


@dataclass(slots=True)
class AgentDependencies:
    """Collaborators one agent run needs."""

    llm: LLMProvider
    manager: MCPManager
    tools: ToolExecutionService
    conversations: ConversationService
    audit: AuditService
    conversation: Conversation
    settings: Settings


# --------------------------------------------------------------------------- #
# 1. Load context
# --------------------------------------------------------------------------- #
async def load_context(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Rebuild the conversation history the model will reason over."""
    history = await deps.conversations.load_history(deps.conversation)

    await deps.audit.record(
        AuditEventType.AGENT_REQUEST_RECEIVED,
        conversation_id=deps.conversation.id,
        user_id=uuid.UUID(state.user_id),
        summary=state.current_request[:500],
        payload={"history_messages": len(history)},
    )
    logger.info(
        "agent context loaded",
        extra=safe_extra({"event": "agent.context_loaded", "history_messages": len(history)}),
    )
    return {"conversation_history": history}


# --------------------------------------------------------------------------- #
# 2. Discover tools
# --------------------------------------------------------------------------- #
async def discover_tools(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Read the current tool catalog from the MCP layer.

    The agent learns what it can do at runtime. Nothing here knows the names of any
    tools, so a server gaining or losing a tool changes the agent's capabilities
    with no code change.

    A viewer is shown only READ tools. That is an ergonomic filter, not the
    enforcement point -- the policy engine still checks every call.
    """
    catalog = deps.manager.catalog
    if state.user_role == UserRole.VIEWER:
        catalog = catalog.readable_only()
    available = list(catalog)

    await deps.audit.record(
        AuditEventType.AGENT_TOOLS_DISCOVERED,
        conversation_id=deps.conversation.id,
        user_id=uuid.UUID(state.user_id),
        summary=f"{len(available)} tools available",
        payload={"tools": [spec.qualified_name for spec in available], **catalog.stats()},
    )

    if not available:
        # No catalog means every MCP server is unreachable. Say so rather than
        # letting the model apologise vaguely for being unable to help.
        return {
            "available_tools": [],
            "errors": [*state.errors, "No MCP tools are currently available."],
        }
    return {"available_tools": available}


# --------------------------------------------------------------------------- #
# 3. Plan (analyse the request and select tools)
# --------------------------------------------------------------------------- #
async def plan(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Ask the model what to do next: call tools, or answer."""
    if state.iteration_budget_exhausted:
        # A model looping on tools is a real failure mode. Stop, and answer with
        # what has already been gathered rather than spending unbounded calls.
        logger.warning(
            "agent hit its iteration limit",
            extra=safe_extra({"event": "agent.iteration_limit", "iterations": state.iteration}),
        )
        return {
            "status": AgentStatus.RUNNING,
            "tool_calls": [],
            "final_response": None,
            "errors": [*state.errors, f"Reached the {state.max_iterations}-step limit."],
        }

    messages = _build_messages(state)
    try:
        response = await deps.llm.complete(messages, tools=state.available_tools or None)
    except LLMError as exc:
        logger.warning(
            "LLM call failed during planning",
            extra=safe_extra({"event": "agent.llm_failed", "error_type": type(exc).__name__}),
        )
        await deps.audit.agent_error(
            error=exc.message,
            conversation_id=deps.conversation.id,
            user_id=uuid.UUID(state.user_id),
        )
        return {
            "status": AgentStatus.FAILED,
            "errors": [*state.errors, exc.message],
            "final_response": (
                "I could not reach the language model to process that request. Please try again."
            ),
        }

    await deps.conversations.add_assistant_message(
        deps.conversation, content=response.content, tool_calls=response.tool_calls
    )
    await deps.audit.record(
        AuditEventType.AGENT_DECISION,
        conversation_id=deps.conversation.id,
        user_id=uuid.UUID(state.user_id),
        summary=f"selected {len(response.tool_calls)} tool(s)",
        payload={
            "tools": [call.name for call in response.tool_calls],
            "iteration": state.iteration + 1,
            "has_content": bool(response.content),
        },
    )

    return {
        "iteration": state.iteration + 1,
        "tool_calls": response.tool_calls,
        "selected_tools": [*state.selected_tools, *(call.name for call in response.tool_calls)],
        "final_response": response.content if not response.tool_calls else None,
        "conversation_history": [
            *state.conversation_history,
            LLMMessage.assistant(content=response.content, tool_calls=response.tool_calls),
        ],
    }


# --------------------------------------------------------------------------- #
# 4. Execute tools
# --------------------------------------------------------------------------- #
async def execute_tools(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Run the proposed calls through the authorisation gateway.

    Calls run **sequentially**. Later steps routinely depend on earlier results
    (draft an email addressed to the leads just fetched), and running writes
    concurrently would make the order of side effects nondeterministic.

    The first call that requires approval stops the run. Continuing would either
    execute later steps that assume the paused one happened, or queue a second
    approval before the first was answered.
    """
    context = PolicyContext(
        user_id=state.user_id, user_role=state.user_role, conversation_id=state.conversation_id
    )
    conversation_uuid = deps.conversation.id
    user_uuid = uuid.UUID(state.user_id)

    results: list[ToolResult] = []
    history: list[LLMMessage] = list(state.conversation_history)
    pending: PendingApproval | None = None
    errors: list[str] = list(state.errors)

    for proposed in state.tool_calls:
        call = ToolCall(id=proposed.id, tool_name=proposed.name, arguments=proposed.arguments)
        try:
            outcome = await deps.tools.execute(
                call, context, conversation_id=conversation_uuid, user_id=user_uuid
            )
        except AppError as exc:
            result = ToolResult.failure(call, exc.message)
            results.append(result)
            errors.append(exc.message)
            await deps.conversations.add_tool_result(deps.conversation, result)
            history.append(
                LLMMessage.tool(tool_call_id=call.id, name=call.tool_name, content=exc.message)
            )
            continue

        if outcome.needs_approval and outcome.approval is not None:
            approval = outcome.approval
            pending = PendingApproval(
                approval_id=str(approval.id),
                tool_name=approval.tool_name,
                summary=approval.summary,
                risk_reason=approval.risk_reason,
                expires_at=approval.expires_at,
                arguments=dict(approval.arguments),
            )
            notice = (
                f"This action needs your approval before it can run: {approval.summary} "
                f"({approval.risk_reason})"
            )
            result = ToolResult.failure(call, notice)
            results.append(result)
            await deps.conversations.add_tool_result(deps.conversation, result)
            history.append(
                LLMMessage.tool(tool_call_id=call.id, name=call.tool_name, content=notice)
            )
            break

        result = outcome.agent_visible_result()
        results.append(result)
        if not result.success:
            errors.append(result.error or f"{call.tool_name} failed.")
        await deps.conversations.add_tool_result(deps.conversation, result)
        history.append(
            LLMMessage.tool(
                tool_call_id=call.id,
                name=call.tool_name,
                content=_render_for_model(result),
            )
        )

    return {
        "tool_results": [*state.tool_results, *results],
        "conversation_history": history,
        "tool_calls": [],
        "pending_approval": pending,
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# 5. Validate results
# --------------------------------------------------------------------------- #
async def validate_results(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Check what came back and decide whether the run can continue.

    Validation is structural, not semantic: it confirms every proposed call produced
    a result and that the run has not degenerated into repeated failure. Judging
    whether the *content* answers the user is the model's job, on the next pass.
    """
    if state.pending_approval is not None:
        return {"status": AgentStatus.AWAITING_APPROVAL}

    recent = (
        state.tool_results[-len(state.tool_calls) :] if state.tool_calls else state.tool_results
    )
    failures = [result for result in recent if not result.success]

    if failures and len(failures) == len(recent) and state.iteration >= 2:
        # Everything failed, twice over. Another identical pass is unlikely to help.
        logger.warning(
            "all tool calls failed; ending the run",
            extra=safe_extra({"event": "agent.repeated_failures", "failures": len(failures)}),
        )
        return {
            "status": AgentStatus.RUNNING,
            "errors": [*state.errors, "Repeated tool failures; stopping."],
            "final_response": None,
        }
    return {}


# --------------------------------------------------------------------------- #
# 6. Respond
# --------------------------------------------------------------------------- #
async def respond(state: AgentState, deps: AgentDependencies) -> dict[str, Any]:
    """Produce the user-facing answer and close out the run."""
    # A failure recorded upstream (an LLM outage, say) must survive this node. It
    # would otherwise be overwritten with COMPLETED and the caller would be told the
    # request succeeded while holding an apology as the "answer".
    if state.status == AgentStatus.FAILED:
        status = AgentStatus.FAILED
    elif state.pending_approval:
        status = AgentStatus.AWAITING_APPROVAL
    else:
        status = AgentStatus.COMPLETED
    response = state.final_response

    if not response:
        response = await _summarise_without_model(state, deps)

    if state.pending_approval is not None:
        response = f"{response.rstrip()}\n\n{_approval_notice(state.pending_approval)}"
        await deps.conversations.add_assistant_message(
            deps.conversation, content=response, extra_metadata={"approval_required": True}
        )

    await deps.audit.record(
        AuditEventType.AGENT_RESPONSE,
        outcome=AuditOutcome.PENDING if state.pending_approval else AuditOutcome.SUCCESS,
        conversation_id=deps.conversation.id,
        user_id=uuid.UUID(state.user_id),
        summary=response[:500],
        payload=state.summary(),
    )
    logger.info(
        "agent run finished", extra=safe_extra({"event": "agent.finished", **state.summary()})
    )
    return {"final_response": response, "status": status}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_messages(state: AgentState) -> list[LLMMessage]:
    """Assemble the message list for a planning call."""
    system = build_system_prompt(
        read_only=state.user_role == UserRole.VIEWER,
        awaiting_approval=state.pending_approval is not None,
    )
    return [LLMMessage.system(system), *state.conversation_history]


def _render_for_model(result: ToolResult) -> str:
    """Serialise a tool result for the model to read."""
    import json

    if result.structured_content is not None:
        return json.dumps(result.structured_content, default=str)
    return result.content or (result.error or "")


def _approval_notice(pending: PendingApproval) -> str:
    return (
        f"**Approval required.** {pending.summary}\n"
        f"Reason: {pending.risk_reason}\n"
        f"Confirm with: POST /api/v1/approvals/{pending.approval_id}/confirm "
        f"(expires {pending.expires_at:%Y-%m-%d %H:%M} UTC)."
    )


async def _summarise_without_model(state: AgentState, deps: AgentDependencies) -> str:
    """Compose a fallback answer when the model produced no closing text.

    Happens when the iteration budget runs out mid-loop, or a provider returns tool
    calls and never a final message. Returning the gathered facts is more useful
    than an empty response.
    """
    if state.pending_approval is not None:
        return "I have prepared this action and it is waiting for your approval."

    successes = state.successful_results()
    if not successes and state.errors:
        return "I could not complete that request. " + " ".join(state.errors[-2:])
    if not successes:
        return "I was not able to find anything for that request."

    lines = [f"- {result.tool_name}: {_condense(result)}" for result in successes[-5:]]
    return "Here is what I found:\n" + "\n".join(lines)


def _condense(result: ToolResult) -> str:
    """One short line describing a successful tool result."""
    payload = result.structured_content
    if isinstance(payload, dict):
        if "count" in payload:
            return f"{payload['count']} record(s)"
        if "headline" in payload:
            return str(payload["headline"])
        keys = ", ".join(list(payload)[:4])
        return f"returned {keys}"
    return (result.content or "completed")[:160]
