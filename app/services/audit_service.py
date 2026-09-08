"""Audit service -- the tamper-evident record of what the system decided and did.

Two rules govern everything written here:

1. **Redact before persisting.** Every payload passes through
   :func:`app.core.security.redact`, so a credential that appeared in a tool
   argument never reaches the database. The audit trail is frequently the *most*
   widely read table in a system like this; it must not become the place secrets
   accumulate.
2. **Never let auditing break the request.** A failure to write an audit row is
   logged loudly but does not propagate. Losing an audit entry is bad; failing a
   user's request because the audit table was momentarily unavailable is worse, and
   the structured log still carries the event.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import current_request_id, get_logger, safe_extra
from app.core.security import redact, truncate
from app.models.database.enums import AuditEventType, AuditOutcome, PermissionLevel
from app.repositories.audit_repository import AuditRepository

logger = get_logger(__name__)

#: Ceiling on a single stored payload, so one large tool result cannot bloat the table.
MAX_PAYLOAD_CHARS = 8000


class AuditService:
    """Writes structured audit entries and mirrors them to the log stream.

    Each entry is committed immediately rather than riding on the caller's
    transaction. Two reasons, both important:

    * **Audit rows must survive failure.** If the request later rolls back, the
      record of what was attempted -- including a blocked or failed action -- is
      exactly what an investigator needs. An audit trail that disappears alongside
      the incident it documents is not an audit trail.
    * **No long transactions across external I/O.** An agent run dispatches tool
      calls to other processes and to the network. Holding a write transaction open
      for that whole time pins a connection and takes database locks for the
      duration of work the database has nothing to do with.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = AuditRepository(session)

    async def record(
        self,
        event_type: AuditEventType,
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        conversation_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        approval_id: uuid.UUID | None = None,
        tool_name: str | None = None,
        server_name: str | None = None,
        permission_level: PermissionLevel | None = None,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Persist one audit entry. Never raises."""
        safe_payload = _sanitise(payload)

        logger.info(
            "audit: %s",
            event_type.value,
            extra=safe_extra(
                {
                    "event": event_type.value,
                    "outcome": outcome.value,
                    "tool": tool_name,
                    "server": server_name,
                    "permission": permission_level.value if permission_level else None,
                    "duration_ms": duration_ms,
                    "audit_summary": summary,
                }
            ),
        )

        try:
            await self._repository.record(
                event_type=event_type,
                outcome=outcome,
                request_id=current_request_id(),
                conversation_id=conversation_id,
                user_id=user_id,
                approval_id=approval_id,
                tool_name=tool_name,
                server_name=server_name,
                permission_level=permission_level,
                summary=truncate(summary, 2000) if summary else None,
                payload=safe_payload,
                error=truncate(error, 2000) if error else None,
                duration_ms=duration_ms,
            )
            await self._session.commit()
        except Exception:
            # Deliberately swallowed -- see the module docstring.
            logger.exception(
                "failed to persist an audit entry",
                extra=safe_extra(
                    {"event": "audit.write_failed", "audited_event": event_type.value}
                ),
            )

    # ------------------------------------------------------------- convenience --
    async def tool_blocked(
        self,
        *,
        tool_name: str,
        reason: str,
        permission_level: PermissionLevel,
        conversation_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.record(
            AuditEventType.TOOL_CALL_BLOCKED,
            outcome=AuditOutcome.BLOCKED,
            tool_name=tool_name,
            permission_level=permission_level,
            conversation_id=conversation_id,
            user_id=user_id,
            summary=reason,
            payload=payload,
        )

    async def agent_error(
        self,
        *,
        error: str,
        conversation_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.record(
            AuditEventType.AGENT_ERROR,
            outcome=AuditOutcome.FAILURE,
            conversation_id=conversation_id,
            user_id=user_id,
            error=error,
            payload=payload,
        )

    async def entries_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int = 100
    ) -> list[Any]:
        return list(await self._repository.list_for_conversation(conversation_id, limit=limit))


def _sanitise(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact and bound a payload before it is stored."""
    if payload is None:
        return None
    redacted = redact(payload)
    if not isinstance(redacted, dict):
        return {"value": truncate(str(redacted), MAX_PAYLOAD_CHARS)}
    return {key: truncate(value, MAX_PAYLOAD_CHARS) for key, value in redacted.items()}
