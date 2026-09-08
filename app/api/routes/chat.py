"""Chat endpoint -- the main entry point to the assistant."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import AgentDep, ConversationDep, CurrentUser
from app.core.logging import bind_request_context, get_logger
from app.models.schemas.api import ChatRequest, ChatResponse, ErrorResponse, PendingApprovalOut

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Send a request to the assistant",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        403: {"model": ErrorResponse, "description": "The action was blocked by policy"},
        503: {"model": ErrorResponse, "description": "No MCP servers are available"},
    },
    response_description=(
        "The assistant's reply. When `approval_required` is true nothing has been "
        "executed yet -- confirm the approval to proceed."
    ),
)
async def chat(
    payload: ChatRequest,
    user: CurrentUser,
    conversations: ConversationDep,
    agent: AgentDep,
) -> ChatResponse:
    """Process a request through the agent.

    The agent discovers the available MCP tools, decides which to call, and executes
    them subject to the permission policy. A high-risk action does **not** run: the
    response comes back with ``approval_required`` and the exact arguments that are
    waiting, for confirmation via ``POST /approvals/{id}/confirm``.
    """
    conversation = await conversations.get_or_create(
        user=user, conversation_id=payload.conversation_id, title=payload.message
    )
    # Bind ids now so every downstream log line and audit row correlates, including
    # those written inside the agent graph.
    bind_request_context(conversation_id=str(conversation.id), user_id=str(user.id))

    result = await agent.run(user=user, conversation=conversation, message=payload.message)

    return ChatResponse(
        conversation_id=result.conversation_id,
        response=result.response,
        status=_map_status(result.status.value),
        approval_required=result.approval_required,
        approval=(
            PendingApprovalOut(**result.pending_approval.model_dump())
            if result.pending_approval
            else None
        ),
        tools_called=result.tools_called,
        errors=result.errors,
        iterations=result.iterations,
        duration_ms=result.duration_ms,
    )


def _map_status(status_value: str) -> str:
    """Collapse internal run states onto the three the API publishes."""
    return (
        status_value
        if status_value in ("completed", "awaiting_approval", "failed")
        else "completed"
    )
