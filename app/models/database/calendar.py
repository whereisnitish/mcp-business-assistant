"""Calendar event model.

Backs the calendar MCP tools that answer the "show me today's meetings" use case.
Like the CRM models it carries ``source_system``/``external_id`` so a real provider
(Google Calendar, Microsoft 365) can be substituted behind ``CalendarInterface``
without changing the MCP tool contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONColumn, UTCDateTime


class CalendarEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A scheduled meeting or block of time."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        Index("ix_calendar_events_owner_start", "owner_id", "starts_at"),
        Index("ix_calendar_events_source_external", "source_system", "external_id", unique=True),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(300))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attendees: Mapped[list[str] | None] = mapped_column(JSONColumn)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )

    source_system: Mapped[str] = mapped_column(String(50), default="local", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    @property
    def duration_minutes(self) -> int:
        start = self.starts_at if self.starts_at.tzinfo else self.starts_at.replace(tzinfo=UTC)
        end = self.ends_at if self.ends_at.tzinfo else self.ends_at.replace(tzinfo=UTC)
        return max(0, int((end - start).total_seconds() // 60))
