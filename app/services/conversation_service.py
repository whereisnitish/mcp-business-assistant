"""Conversation service: transcript persistence and replay.

Owns the translation between what is stored (``Message`` rows) and what the model
sees (:class:`~app.providers.llm.base.LLMMessage`). Persisting the full turn
structure -- assistant tool calls and their matching tool results, not just prose --
is what allows a conversation to be resumed correctly: a follow-up like "send that
one too" needs the earlier tool results in context to mean anything.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.mcp.types import ToolResult
from app.models.database.conversation import Conversation, Message
from app.models.database.enums import ConversationStatus, MessageRole, UserRole
from app.models.database.user import User
from app.providers.llm.base import LLMMessage, LLMRole, LLMToolCall
from app.repositories.conversation_repository import ConversationRepository


class ConversationService:
    """Creates, loads and appends to conversations.

    Each appended turn is committed as it happens rather than accumulating into one
    transaction spanning the whole agent run. A run dispatches tool calls to other
    processes and to the network; holding a write transaction open across that would
    pin a connection and hold locks for the duration. It also means a transcript
    reflects what actually occurred up to the point of any later failure.
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._repository = ConversationRepository(session)
        self._settings = settings or get_settings()

    async def _persist(self, entity: Any) -> Any:
        """Commit a just-written row and return it."""
        await self._session.commit()
        return entity

    # ----------------------------------------------------------------- loading --
    async def get_or_create(
        self, *, user: User, conversation_id: uuid.UUID | str | None, title: str | None = None
    ) -> Conversation:
        """Resume a conversation, or start a new one when no id is supplied."""
        if conversation_id is None:
            return await self._persist(
                await self._repository.create_conversation(
                    user_id=user.id, title=_derive_title(title)
                )
            )
        conversation = await self._repository.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} was not found.")
        self._assert_visible(conversation, user)
        return conversation

    async def get_for_user(self, conversation_id: uuid.UUID | str, *, user: User) -> Conversation:
        """Load a conversation with its messages, enforcing ownership."""
        conversation = await self._repository.get_with_messages(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} was not found.")
        self._assert_visible(conversation, user)
        return conversation

    def _assert_visible(self, conversation: Conversation, user: User) -> None:
        """Ownership check.

        Conversations contain business data and pending actions, so one user must
        not be able to read another's by guessing an id. Admins may read any.
        """
        if user.role != UserRole.ADMIN and conversation.user_id != user.id:
            raise AuthorizationError("You do not have access to this conversation.")

    # --------------------------------------------------------------- appending --
    async def add_user_message(self, conversation: Conversation, content: str) -> Message:
        return await self._persist(
            await self._repository.add_message(
                conversation_id=conversation.id, role=MessageRole.USER, content=content
            )
        )

    async def add_assistant_message(
        self,
        conversation: Conversation,
        *,
        content: str | None,
        tool_calls: list[LLMToolCall] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Message:
        return await self._persist(
            await self._repository.add_message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=content,
                tool_calls=[
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in (tool_calls or [])
                ]
                or None,
                extra_metadata=extra_metadata,
            )
        )

    async def add_tool_result(self, conversation: Conversation, result: ToolResult) -> Message:
        """Record a tool result as its own turn, linked to the call it answers."""
        return await self._persist(
            await self._repository.add_message(
                conversation_id=conversation.id,
                role=MessageRole.TOOL,
                content=_render_tool_content(result),
                tool_call_id=result.tool_call_id,
                tool_name=result.tool_name,
                extra_metadata={"success": result.success, "duration_ms": result.duration_ms},
            )
        )

    async def set_status(
        self, conversation: Conversation, status: ConversationStatus
    ) -> Conversation:
        return await self._persist(await self._repository.set_status(conversation, status))

    # ------------------------------------------------------------------ replay --
    async def load_history(
        self, conversation: Conversation, *, limit: int | None = None
    ) -> list[LLMMessage]:
        """Rebuild the model-facing history for a conversation.

        Trimming to the most recent turns bounds prompt size and cost. The window
        can cut between an assistant tool call and its result, which some providers
        reject, so :func:`_repair_dangling_tool_messages` drops orphans on the way out.
        """
        rows = await self._repository.list_messages(
            conversation.id, limit=limit or self._settings.agent_history_limit
        )
        return _repair_dangling_tool_messages([_to_llm_message(row) for row in rows])


# --------------------------------------------------------------------------- #
# Mapping helpers
# --------------------------------------------------------------------------- #
def _to_llm_message(row: Message) -> LLMMessage:
    tool_calls = [
        LLMToolCall(id=call["id"], name=call["name"], arguments=call.get("arguments", {}))
        for call in (row.tool_calls or [])
        if isinstance(call, dict) and call.get("id") and call.get("name")
    ]
    return LLMMessage(
        role=LLMRole(row.role.value),
        content=row.content,
        tool_calls=tool_calls,
        tool_call_id=row.tool_call_id,
        name=row.tool_name,
    )


def _render_tool_content(result: ToolResult) -> str:
    """Serialise a tool result for the model.

    Structured content is preferred and rendered as compact JSON: models parse JSON
    far more reliably than prose, and it keeps numbers exact.
    """
    if result.structured_content is not None:
        return json.dumps(result.structured_content, default=str)
    return result.content or ("" if result.success else (result.error or "The tool failed."))


def _repair_dangling_tool_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Drop tool results whose originating assistant call fell outside the window.

    A ``tool`` message with no preceding ``tool_calls`` entry is a protocol error for
    OpenAI-compatible APIs and causes a 400. Since trimming history can orphan one,
    they are removed rather than sent.
    """
    known_call_ids: set[str] = set()
    repaired: list[LLMMessage] = []

    for message in messages:
        if message.role == LLMRole.ASSISTANT:
            known_call_ids.update(call.id for call in message.tool_calls)
            repaired.append(message)
        elif message.role == LLMRole.TOOL:
            if message.tool_call_id and message.tool_call_id in known_call_ids:
                repaired.append(message)
        else:
            repaired.append(message)
    return repaired


def _derive_title(text: str | None) -> str | None:
    """Use the opening request as the conversation title, trimmed."""
    if not text:
        return None
    cleaned = " ".join(text.split())
    return cleaned[:120] + ("..." if len(cleaned) > 120 else "")
