"""Audit log model.

Every agent decision, policy verdict, tool call, approval event and error lands
here. The audit trail is what makes an autonomous system reviewable: given a
``request_id`` you can reconstruct exactly which tools the agent considered, which
the policy engine allowed, what arguments were sent and what came back.

Payloads are redacted by :class:`~app.services.audit_service.AuditService` before
they reach this table -- credentials never touch the database.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONColumn
from app.models.database.enums import AuditEventType, AuditOutcome, PermissionLevel, str_enum_column


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An append-only record of something the system decided or did.

    Rows are never updated or deleted by application code. Foreign keys use
    ``ON DELETE SET NULL`` so that removing a user or conversation cannot erase the
    history of what was done in their name.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_event_created", "event_type", "created_at"),
        Index("ix_audit_logs_conversation_created", "conversation_id", "created_at"),
        Index("ix_audit_logs_request", "request_id", "created_at"),
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        str_enum_column(AuditEventType, "audit_event_type"), nullable=False, index=True
    )
    outcome: Mapped[AuditOutcome] = mapped_column(
        str_enum_column(AuditOutcome, "audit_outcome"), default=AuditOutcome.SUCCESS, nullable=False
    )

    # --- correlation -------------------------------------------------------- #
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="SET NULL"), index=True
    )

    # --- subject ------------------------------------------------------------ #
    tool_name: Mapped[str | None] = mapped_column(String(128), index=True)
    server_name: Mapped[str | None] = mapped_column(String(64))
    permission_level: Mapped[PermissionLevel | None] = mapped_column(
        str_enum_column(PermissionLevel, "permission_level")
    )

    # --- detail ------------------------------------------------------------- #
    summary: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
