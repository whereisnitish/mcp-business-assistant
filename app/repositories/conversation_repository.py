"""Conversation and message persistence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.database.conversation import Conversation, Message
from app.models.database.enums import ConversationStatus, MessageRole
from app.repositories.base import BaseRepository, clamp_limit


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    async def create_conversation(
        self, *, user_id: uuid.UUID, title: str | None = None
    ) -> Conversation:
        return await self.add(Conversation(user_id=user_id, title=title))

    async def get_with_messages(self, conversation_id: uuid.UUID | str) -> Conversation | None:
        identifier = self._coerce_uuid(conversation_id)
        if identifier is None:
            return None
        stmt = (
            select(Conversation)
            .where(Conversation.id == identifier)
            .options(selectinload(Conversation.messages))
        )
        return (await self.session.scalars(stmt)).unique().one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int | None = None, offset: int = 0
    ) -> Sequence[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(clamp_limit(limit))
            .offset(max(0, offset))
        )
        return (await self.session.scalars(stmt)).all()

    async def set_status(
        self, conversation: Conversation, status: ConversationStatus
    ) -> Conversation:
        conversation.status = status
        await self.session.flush()
        return conversation

    # ---------------------------------------------------------------- messages --
    async def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """Next monotonic turn number.

        A unique index on ``(conversation_id, sequence)`` backs this: if two
        concurrent requests race for the same slot, the second one fails on the
        constraint rather than silently interleaving the transcript.
        """
        stmt = select(func.coalesce(func.max(Message.sequence), 0)).where(
            Message.conversation_id == conversation_id
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def add_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            sequence=await self.next_sequence(conversation_id),
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            extra_metadata=extra_metadata,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_messages(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> Sequence[Message]:
        """Return the transcript in chronological order.

        The newest *N* turns are selected in the database, then reversed in Python,
        so a long-running conversation never loads its full history to show a window.
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
            .limit(clamp_limit(limit, default=50))
        )
        return list(reversed((await self.session.scalars(stmt)).all()))
