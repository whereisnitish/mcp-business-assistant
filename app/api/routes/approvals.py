"""Approval endpoints -- where a human decides whether a proposed action runs.

These are the only routes that can cause a HIGH_RISK tool to execute, and they are
reachable only by an authenticated user. The agent cannot call them: there is no
tool that maps to them, and nothing in the model's output path can reach a
FastAPI route.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.dependencies import AgentDep, ApprovalDep, ConversationDep, CurrentUser
from app.core.logging import get_logger
from app.models.database.approval import ApprovalRequest
from app.models.schemas.api import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalListResponse,
    ApprovalOut,
    ErrorResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _to_out(request: ApprovalRequest) -> ApprovalOut:
    return ApprovalOut(
        id=str(request.id),
        conversation_id=str(request.conversation_id),
        tool_name=request.tool_name,
        server_name=request.server_name,
        permission_level=request.permission_level,
        status=request.status,
        summary=request.summary,
        risk_reason=request.risk_reason,
        arguments=dict(request.arguments),
        created_at=request.created_at,
        expires_at=request.expires_at,
        decided_at=request.decided_at,
        executed_at=request.executed_at,
        error=request.error,
    )


@router.get(
    "",
    response_model=ApprovalListResponse,
    summary="List approvals awaiting a decision",
)
async def list_approvals(
    user: CurrentUser,
    approvals: ApprovalDep,
    conversation_id: str | None = Query(default=None, description="Restrict to one conversation."),
    limit: int = Query(default=25, ge=1, le=100),
) -> ApprovalListResponse:
    """Pending approvals you are able to decide.

    Administrators see every pending request; everyone else sees only their own.
    """
    scope: UUID | None = None
    if conversation_id:
        try:
            scope = UUID(conversation_id)
        except ValueError:
            # A malformed id filters to nothing rather than 422-ing: this is a
            # convenience filter, not part of the resource's identity.
            return ApprovalListResponse(approvals=[], count=0)

    pending = await approvals.list_pending(user=user, conversation_id=scope, limit=limit)
    return ApprovalListResponse(approvals=[_to_out(item) for item in pending], count=len(pending))


@router.get(
    "/{approval_id}",
    response_model=ApprovalOut,
    summary="Inspect one approval request",
    responses={404: {"model": ErrorResponse, "description": "No such approval request"}},
)
async def get_approval(approval_id: str, user: CurrentUser, approvals: ApprovalDep) -> ApprovalOut:
    """Fetch a single approval, including the exact arguments awaiting execution."""
    request = await approvals.get(approval_id)
    approvals.assert_can_view(request, user)
    return _to_out(request)


@router.post(
    "/{approval_id}/confirm",
    response_model=ApprovalDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm a pending action and execute it",
    responses={
        403: {"model": ErrorResponse, "description": "You may not decide this request"},
        404: {"model": ErrorResponse, "description": "No such approval request"},
        409: {"model": ErrorResponse, "description": "Already decided, expired, or tampered with"},
    },
)
async def confirm_approval(
    approval_id: str,
    user: CurrentUser,
    approvals: ApprovalDep,
    conversations: ConversationDep,
    agent: AgentDep,
    payload: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Approve and run a pending action.

    The arguments executed come from the stored approval record, not from this
    request, so confirming cannot alter what happens. The transition to APPROVED is
    a conditional database update, which makes a replayed confirmation a no-op
    rather than a second execution.
    """
    request, grant = await approvals.confirm(
        approval_id, approver=user, note=payload.note if payload else None
    )
    conversation = await conversations.get_or_create(
        user=user, conversation_id=str(request.conversation_id)
    )

    result = await agent.resume_after_approval(user=user, conversation=conversation, grant=grant)
    refreshed = await approvals.get(request.id)

    return ApprovalDecisionResponse(
        approval=_to_out(refreshed),
        executed=not result.errors,
        conversation_id=result.conversation_id,
        response=result.response,
        error=result.errors[0] if result.errors else None,
    )


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDecisionResponse,
    summary="Reject a pending action",
    responses={
        403: {"model": ErrorResponse, "description": "You may not decide this request"},
        404: {"model": ErrorResponse, "description": "No such approval request"},
        409: {"model": ErrorResponse, "description": "Already decided"},
    },
)
async def reject_approval(
    approval_id: str,
    user: CurrentUser,
    approvals: ApprovalDep,
    payload: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Reject a pending action. It is never executed."""
    request = await approvals.reject(
        approval_id, approver=user, note=payload.note if payload else None
    )
    return ApprovalDecisionResponse(
        approval=_to_out(request),
        executed=False,
        conversation_id=str(request.conversation_id),
        response="The action was rejected and will not be performed.",
    )
