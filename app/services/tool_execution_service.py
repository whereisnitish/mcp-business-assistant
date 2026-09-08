"""Tool execution service -- the single gateway between the agent and MCP.

Every tool call in the system goes through :meth:`ToolExecutionService.execute`.
That is a deliberate architectural constraint: with exactly one path to the MCP
manager, the authorisation check cannot be bypassed by adding a code path that
forgets it. :meth:`app.mcp.manager.MCPManager.call_tool` performs no permission
checking at all, precisely so that this service is the only place where the security
decision lives, and the only place a reviewer must read to verify it.

The pipeline, in order:

    resolve tool -> validate arguments -> policy decision
        -> [DENY]             refuse, audit, return a failure the agent can explain
        -> [APPROVAL, no grant] persist an ApprovalRequest, return APPROVAL_REQUIRED
        -> [APPROVAL, grant]   re-verify the grant against the database, then execute
        -> [ALLOW]            execute
    -> audit the outcome

Note that a grant does not skip the policy step. If the policy would now deny the
call -- the caller's role changed, an argument limit was breached -- it is refused
even with a valid approval in hand.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    MCPServerUnavailableError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from app.core.logging import Timer, get_logger, safe_extra
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall, ToolResult, ToolSpec
from app.mcp.validation import validate_arguments
from app.models.database.approval import ApprovalRequest
from app.models.database.enums import AuditEventType, AuditOutcome, PermissionLevel
from app.security.policy import PolicyContext, PolicyDecision, PolicyEngine
from app.services.approval_service import ApprovalGrant, ApprovalService
from app.services.audit_service import AuditService

logger = get_logger(__name__)


class ExecutionStatus(StrEnum):
    EXECUTED = "executed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What happened to a proposed tool call."""

    status: ExecutionStatus
    call: ToolCall
    result: ToolResult | None = None
    approval: ApprovalRequest | None = None
    decision: PolicyDecision | None = None
    message: str = ""

    @property
    def executed(self) -> bool:
        return self.status == ExecutionStatus.EXECUTED

    @property
    def needs_approval(self) -> bool:
        return self.status == ExecutionStatus.APPROVAL_REQUIRED

    def agent_visible_result(self) -> ToolResult:
        """A result the agent can feed back to the model.

        Denials and approval pauses become failure results carrying an explanation,
        so the model can tell the user what happened instead of silently retrying.
        """
        if self.result is not None:
            return self.result
        return ToolResult.failure(self.call, self.message or "The tool call did not run.")


class ToolExecutionService:
    """Authorises and executes tool calls."""

    def __init__(
        self,
        *,
        manager: MCPManager,
        approvals: ApprovalService,
        audit: AuditService,
        policy: PolicyEngine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._manager = manager
        self._approvals = approvals
        self._audit = audit
        self._settings = settings or get_settings()
        self._policy = policy or PolicyEngine(self._settings)

    async def execute(
        self,
        call: ToolCall,
        context: PolicyContext,
        *,
        grant: ApprovalGrant | None = None,
        conversation_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> ExecutionOutcome:
        """Authorise and (if permitted) run a single tool call."""
        spec = self._manager.catalog.get(call.tool_name)
        if spec is None:
            return await self._deny_unknown_tool(call, conversation_id, user_id)

        if problems := validate_arguments(spec.input_schema, call.arguments):
            return await self._reject_arguments(call, spec, problems, conversation_id, user_id)

        decision = self._policy.evaluate(spec, call.arguments, context)

        if decision.denied:
            return await self._deny(call, spec, decision, conversation_id, user_id)

        if decision.needs_approval:
            if grant is None:
                return await self._request_approval(call, spec, decision, conversation_id, user_id)
            # A grant was supplied: prove it against persisted state, not the object.
            approval = await self._approvals.verify_grant(grant, tool_name=call.tool_name)
            return await self._run(
                call, spec, decision, conversation_id, user_id, approval=approval
            )

        return await self._run(call, spec, decision, conversation_id, user_id)

    # ------------------------------------------------------------------- paths --
    async def _run(
        self,
        call: ToolCall,
        spec: ToolSpec,
        decision: PolicyDecision,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        *,
        approval: ApprovalRequest | None = None,
    ) -> ExecutionOutcome:
        """Dispatch to MCP and record the outcome."""
        await self._audit.record(
            AuditEventType.TOOL_CALL_STARTED,
            outcome=AuditOutcome.PENDING,
            conversation_id=conversation_id,
            user_id=user_id,
            approval_id=approval.id if approval else None,
            tool_name=call.tool_name,
            server_name=spec.server,
            permission_level=spec.permission,
            summary=decision.summary,
            payload={"arguments": call.arguments},
        )

        timer = Timer()
        try:
            result = await self._manager.call_tool(
                call, timeout=self._settings.agent_tool_timeout_seconds
            )
        except (
            ToolTimeoutError,
            ToolNotFoundError,
            MCPServerUnavailableError,
            ToolExecutionError,
        ) as exc:
            # Infrastructure failures: the agent is told, and can report or retry.
            result = ToolResult.failure(call, exc.message, duration_ms=timer.elapsed_ms)
            await self._audit.record(
                AuditEventType.TOOL_CALL_FAILED,
                outcome=AuditOutcome.FAILURE,
                conversation_id=conversation_id,
                user_id=user_id,
                approval_id=approval.id if approval else None,
                tool_name=call.tool_name,
                server_name=spec.server,
                permission_level=spec.permission,
                error=exc.message,
                duration_ms=timer.elapsed_ms,
            )
            if approval is not None:
                await self._approvals.mark_failed(approval, error=exc.message)
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                call=call,
                result=result,
                decision=decision,
                message=exc.message,
            )

        if result.success:
            await self._audit.record(
                AuditEventType.TOOL_CALL_SUCCEEDED,
                conversation_id=conversation_id,
                user_id=user_id,
                approval_id=approval.id if approval else None,
                tool_name=call.tool_name,
                server_name=spec.server,
                permission_level=spec.permission,
                summary=decision.summary,
                payload=result.to_payload(),
                duration_ms=result.duration_ms,
            )
            if approval is not None:
                await self._approvals.mark_executed(approval, result=result.structured_content)
            return ExecutionOutcome(
                status=ExecutionStatus.EXECUTED, call=call, result=result, decision=decision
            )

        error = result.error or "The tool reported an error."
        await self._audit.record(
            AuditEventType.TOOL_CALL_FAILED,
            outcome=AuditOutcome.FAILURE,
            conversation_id=conversation_id,
            user_id=user_id,
            approval_id=approval.id if approval else None,
            tool_name=call.tool_name,
            server_name=spec.server,
            permission_level=spec.permission,
            error=error,
            duration_ms=result.duration_ms,
        )
        if approval is not None:
            await self._approvals.mark_failed(approval, error=error)
        return ExecutionOutcome(
            status=ExecutionStatus.FAILED,
            call=call,
            result=result,
            decision=decision,
            message=error,
        )

    async def _request_approval(
        self,
        call: ToolCall,
        spec: ToolSpec,
        decision: PolicyDecision,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> ExecutionOutcome:
        if conversation_id is None or user_id is None:
            # Without an owner there is nobody to ask, so the only safe answer is no.
            return ExecutionOutcome(
                status=ExecutionStatus.DENIED,
                call=call,
                decision=decision,
                message="This action requires approval, but no conversation context was available.",
            )

        approval = await self._approvals.create_request(
            conversation_id=conversation_id,
            user_id=user_id,
            decision=decision,
            server_name=spec.server,
            arguments=call.arguments,
            tool_call_id=call.id,
        )
        logger.info(
            "tool call paused for human approval",
            extra=safe_extra(
                {
                    "event": "tool.approval_required",
                    "tool": call.tool_name,
                    "approval_id": str(approval.id),
                }
            ),
        )
        return ExecutionOutcome(
            status=ExecutionStatus.APPROVAL_REQUIRED,
            call=call,
            approval=approval,
            decision=decision,
            message=decision.summary,
        )

    async def _deny(
        self,
        call: ToolCall,
        spec: ToolSpec,
        decision: PolicyDecision,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> ExecutionOutcome:
        await self._audit.tool_blocked(
            tool_name=call.tool_name,
            reason=decision.reason,
            permission_level=spec.permission,
            conversation_id=conversation_id,
            user_id=user_id,
            payload={"arguments": call.arguments},
        )
        return ExecutionOutcome(
            status=ExecutionStatus.DENIED,
            call=call,
            decision=decision,
            result=ToolResult.failure(call, f"Blocked by policy: {decision.reason}"),
            message=decision.reason,
        )

    async def _deny_unknown_tool(
        self, call: ToolCall, conversation_id: uuid.UUID | None, user_id: uuid.UUID | None
    ) -> ExecutionOutcome:
        """Refuse a tool that is not in the catalog.

        Models do hallucinate tool names. Refusing by catalog membership means a
        fabricated name can never reach a server, and the message steers the model
        back to real tools.
        """
        available = ", ".join(self._manager.catalog.names[:20]) or "none"
        message = f"Unknown tool {call.tool_name!r}. Available tools are: {available}."
        await self._audit.tool_blocked(
            tool_name=call.tool_name,
            reason="tool is not in the discovered catalog",
            permission_level=PermissionLevel.HIGH_RISK,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        return ExecutionOutcome(
            status=ExecutionStatus.DENIED,
            call=call,
            result=ToolResult.failure(call, message),
            message=message,
        )

    async def _reject_arguments(
        self,
        call: ToolCall,
        spec: ToolSpec,
        problems: list[str],
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> ExecutionOutcome:
        """Refuse arguments that cannot satisfy the tool's schema."""
        message = f"Invalid arguments for {call.tool_name}: {'; '.join(problems)}."
        await self._audit.record(
            AuditEventType.TOOL_CALL_BLOCKED,
            outcome=AuditOutcome.BLOCKED,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=call.tool_name,
            server_name=spec.server,
            permission_level=spec.permission,
            summary="invalid tool arguments",
            payload={"problems": problems, "arguments": call.arguments},
        )
        return ExecutionOutcome(
            status=ExecutionStatus.DENIED,
            call=call,
            result=ToolResult.failure(call, message),
            message=message,
        )

    async def execute_approved(
        self,
        grant: ApprovalGrant,
        context: PolicyContext,
        *,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ExecutionOutcome:
        """Run a previously approved action.

        The arguments come from the grant -- that is, from the stored row -- and not
        from the confirmation request. A caller confirming an approval has no way to
        influence what actually runs.
        """
        call = ToolCall(
            id=f"approved_{grant.approval_id.hex[:8]}",
            tool_name=grant.tool_name,
            arguments=dict(grant.arguments),
        )
        return await self.execute(
            call, context, grant=grant, conversation_id=conversation_id, user_id=user_id
        )

    def describe_available_tools(self) -> list[dict[str, Any]]:
        """Catalog snapshot for the ``/tools`` endpoint."""
        return [
            {
                "name": spec.qualified_name,
                "server": spec.server,
                "description": spec.description,
                "permission": spec.permission.value,
                "requires_approval": spec.permission == PermissionLevel.HIGH_RISK
                or (
                    spec.permission == PermissionLevel.WRITE
                    and self._settings.require_approval_for_writes
                ),
                "input_schema": spec.input_schema,
            }
            for spec in sorted(self._manager.catalog, key=lambda item: item.qualified_name)
        ]
