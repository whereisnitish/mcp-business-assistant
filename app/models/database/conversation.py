"""Conversation and message models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONColumn
from app.models.database.enums import ConversationStatus, MessageRole, str_enum_column

if TYPE_CHECKING:
    from app.models.database.user import User


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single assistant thread belonging to one user."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[ConversationStatus] = mapped_column(
        str_enum_column(ConversationStatus, "conversation_status"),
        default=ConversationStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
        lazy="selectin",
    )


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One turn in a conversation.

    ``tool_calls`` holds the assistant's proposed calls and ``tool_call_id`` links a
    ``TOOL`` message back to the call it answers, mirroring the OpenAI-compatible
    chat format. Persisting the full turn structure -- not just the prose -- is what
    lets a conversation be replayed into the model on the next request.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence", unique=True),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        str_enum_column(MessageRole, "message_role"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
