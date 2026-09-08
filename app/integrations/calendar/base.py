"""Calendar interface."""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from app.integrations.base import BusinessIntegration
from app.models.schemas.domain import CalendarEventDTO


class CalendarInterface(BusinessIntegration):
    """Read and create scheduled events.

    A real deployment would put Google Calendar or Microsoft Graph behind this.
    """

    @abstractmethod
    async def get_events(
        self, *, start: datetime, end: datetime, limit: int = 50
    ) -> list[CalendarEventDTO]:
        """Events overlapping ``[start, end)``, earliest first."""

    @abstractmethod
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
        """Schedule an event and return it as persisted."""

    @abstractmethod
    async def find_conflicts(
        self, *, starts_at: datetime, ends_at: datetime
    ) -> list[CalendarEventDTO]:
        """Existing events overlapping a proposed slot."""
