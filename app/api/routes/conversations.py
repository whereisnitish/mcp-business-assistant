"""Conversation history endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.dependencies import ConversationDep, CurrentUser, DBSession
from app.models.database.conversation import Conversation
from app.models.schemas.api import (
    ConversationListResponse,
    ConversationOut,
    ErrorResponse,
    MessageOut,
)
from app.repositories.conversation_repository import ConversationRepository

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _to_out(conversation: Conversation, *, include_messages: bool) -> ConversationOut:
    messages = sorted(conversation.messages, key=lambda message: message.sequence)
    return ConversationOut(
        id=str(conversation.id),
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=len(messages),
        messages=[
            MessageOut(
                id=str(message.id),
                sequence=message.sequence,
                role=message.role,
                content=message.content,
                tool_name=message.tool_name,
                tool_calls=message.tool_calls,
                created_at=message.created_at,
            )
            for message in messages
        ]
        if include_messages
        else [],
    )


@router.get(
    "",
    response_model=ConversationListResponse,
    summary="List your conversations",
)
async def list_conversations(
    user: CurrentUser,
    session: DBSession,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ConversationListResponse:
    """Your conversations, most recently updated first. Messages are not included."""
    rows = await ConversationRepository(session).list_for_user(user.id, limit=limit, offset=offset)
    return ConversationListResponse(
        conversations=[_to_out(row, include_messages=False) for row in rows], count=len(rows)
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationOut,
    summary="Retrieve a conversation and its full transcript",
    responses={
        403: {"model": ErrorResponse, "description": "The conversation belongs to another user"},
        404: {"model": ErrorResponse, "description": "No such conversation"},
    },
)
async def get_conversation(
    conversation_id: str, user: CurrentUser, conversations: ConversationDep
) -> ConversationOut:
    """Full transcript, including tool calls and their results.

    Ownership is enforced in the service layer: a conversation can contain business
    data and the arguments of pending actions, so guessing an id must not reveal
    another user's thread.
    """
    conversation = await conversations.get_for_user(conversation_id, user=user)
    return _to_out(conversation, include_messages=True)
