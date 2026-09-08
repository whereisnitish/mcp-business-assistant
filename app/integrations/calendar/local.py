"""PostgreSQL-backed calendar."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, safe_extra
from app.integrations.base import IntegrationError
from app.integrations.calendar.base import CalendarInterface
from app.models.database.calendar import CalendarEvent
from app.models.schemas.domain import CalendarEventDTO
from app.repositories.calendar_repository import CalendarRepository

logger = get_logger(__name__)


def to_event_dto(event: CalendarEvent) -> CalendarEventDTO:
    return CalendarEventDTO(
        id=str(event.id),
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        all_day=event.all_day,
        attendees=list(event.attendees or []),
        duration_minutes=event.duration_minutes,
        lead_id=str(event.lead_id) if event.lead_id else None,
    )


class LocalCalendarRepository(CalendarInterface):
    """Calendar backed by the local ``calendar_events`` table."""

    provider_name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._events = CalendarRepository(session)

    async def health_check(self) -> bool:
        await self._events.count()
        return True

    async def get_events(
        self, *, start: datetime, end: datetime, limit: int = 50
    ) -> list[CalendarEventDTO]:
        rows = await self._events.list_events(start=start, end=end, limit=limit)
        return [to_event_dto(row) for row in rows]

    async def create_event(
        self,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        lead_id: str | None = None,
    ) -> CalendarEventDTO:
        if ends_at <= starts_at:
            raise IntegrationError("calendar", "the event must end after it starts")

        row = await self._events.create_event(
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            description=description,
            location=location,
            attendees=attendees,
            lead_id=_maybe_uuid(lead_id),
        )
        logger.info(
            "calendar event created",
            extra=safe_extra({"event": "calendar.event_created", "event_id": str(row.id)}),
        )
        return to_event_dto(row)

    async def find_conflicts(
        self, *, starts_at: datetime, ends_at: datetime
    ) -> list[CalendarEventDTO]:
        rows = await self._events.find_conflicts(starts_at=starts_at, ends_at=ends_at)
        return [to_event_dto(row) for row in rows]


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
