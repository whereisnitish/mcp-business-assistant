"""Calendar persistence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select

from app.models.database.calendar import CalendarEvent
from app.repositories.base import BaseRepository, clamp_limit


class CalendarRepository(BaseRepository[CalendarEvent]):
    model = CalendarEvent

    async def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        owner_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> Sequence[CalendarEvent]:
        """Events overlapping the half-open window ``[start, end)``.

        Overlap -- not containment -- is the correct test: a meeting that began
        before the window and is still running is part of "today's schedule".
        """
        stmt = select(CalendarEvent).where(
            CalendarEvent.starts_at < end,
            CalendarEvent.ends_at > start,
        )
        if owner_id is not None:
            stmt = stmt.where(CalendarEvent.owner_id == owner_id)
        stmt = stmt.order_by(CalendarEvent.starts_at.asc()).limit(clamp_limit(limit))
        return (await self.session.scalars(stmt)).all()

    async def create_event(
        self,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        owner_id: uuid.UUID | None = None,
        lead_id: uuid.UUID | None = None,
        all_day: bool = False,
    ) -> CalendarEvent:
        event = CalendarEvent(
            title=title.strip(),
            starts_at=starts_at,
            ends_at=ends_at,
            description=description,
            location=location,
            attendees=attendees,
            owner_id=owner_id,
            lead_id=lead_id,
            all_day=all_day,
        )
        return await self.add(event)

    async def find_conflicts(
        self, *, starts_at: datetime, ends_at: datetime, owner_id: uuid.UUID | None = None
    ) -> Sequence[CalendarEvent]:
        """Existing events that overlap a proposed slot."""
        return await self.list_events(start=starts_at, end=ends_at, owner_id=owner_id, limit=10)
