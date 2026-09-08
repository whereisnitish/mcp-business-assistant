"""Calendar MCP server.

Answers scheduling questions -- "show me today's meetings" -- and books new events.
``get_todays_meetings`` exists as a distinct tool rather than making the agent
compute today's UTC bounds and pass them to ``get_events``: a date arithmetic step
delegated to a language model is a date arithmetic step that will eventually be
wrong, and the question is common enough to deserve its own contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.integrations.factory import build_calendar
from app.models.schemas.domain import CalendarEventDTO, CalendarEventListDTO
from mcp_servers.common.runtime import (
    build_server,
    day_bounds,
    ensure_utc,
    run_tool,
    tool_error,
    tool_session,
)

logger = get_logger(__name__)

INSTRUCTIONS = """\
Calendar tools for meetings and scheduling.

get_todays_meetings answers "what is on my calendar today" directly. get_events
covers any other window, given ISO-8601 timestamps. create_event books a new
meeting and reports any conflicting events it overlaps. All times are UTC; a
timestamp without a timezone is read as UTC.
"""

server = build_server("calendar", instructions=INSTRUCTIONS)


@server.tool(
    description=(
        "List every meeting scheduled for today (UTC), earliest first. "
        "Use this for 'what meetings do I have today' and similar questions."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_todays_meetings(
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum events to return.")] = 50,
) -> CalendarEventListDTO:
    async def _run() -> CalendarEventListDTO:
        start, end = day_bounds(datetime.now(UTC).date())
        async with tool_session() as session:
            events = await build_calendar(session).get_events(start=start, end=end, limit=limit)
            return CalendarEventListDTO(
                events=events, count=len(events), range_start=start, range_end=end
            )

    return await run_tool("get_todays_meetings", _run)


@server.tool(
    description=(
        "List calendar events overlapping a time window. Defaults to the next 7 days "
        "when no window is given."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_events(
    start: Annotated[
        datetime | None, Field(description="Window start, ISO-8601. Defaults to now.")
    ] = None,
    end: Annotated[
        datetime | None, Field(description="Window end, ISO-8601. Defaults to 7 days after start.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum events to return.")] = 50,
) -> CalendarEventListDTO:
    async def _run() -> CalendarEventListDTO:
        window_start = ensure_utc(start) if start else datetime.now(UTC)
        window_end = ensure_utc(end) if end else window_start + timedelta(days=7)
        if window_end <= window_start:
            raise tool_error("The end of the window must be after its start.")

        async with tool_session() as session:
            events = await build_calendar(session).get_events(
                start=window_start, end=window_end, limit=limit
            )
            return CalendarEventListDTO(
                events=events, count=len(events), range_start=window_start, range_end=window_end
            )

    return await run_tool("get_events", _run)


@server.tool(
    description=(
        "Book a calendar event. Returns the created event; any existing events it "
        "overlaps are reported in its description of conflicts."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
async def create_event(
    title: Annotated[str, Field(min_length=1, max_length=300, description="Meeting title.")],
    starts_at: Annotated[datetime, Field(description="Start time, ISO-8601.")],
    ends_at: Annotated[datetime, Field(description="End time, ISO-8601.")],
    description: Annotated[
        str | None, Field(max_length=5000, description="Agenda or notes.")
    ] = None,
    location: Annotated[
        str | None, Field(max_length=300, description="Location or meeting link.")
    ] = None,
    attendees: Annotated[list[str] | None, Field(description="Attendee email addresses.")] = None,
    lead_id: Annotated[str | None, Field(description="Related CRM lead identifier.")] = None,
) -> CalendarEventDTO:
    async def _run() -> CalendarEventDTO:
        start = ensure_utc(starts_at)
        end = ensure_utc(ends_at)
        if end <= start:
            raise tool_error("The event must end after it starts.")

        async with tool_session() as session:
            calendar = build_calendar(session)
            conflicts = await calendar.find_conflicts(starts_at=start, ends_at=end)
            event = await calendar.create_event(
                title=title,
                starts_at=start,
                ends_at=end,
                description=description,
                location=location,
                attendees=attendees,
                lead_id=lead_id,
            )
            if conflicts:
                logger.info(
                    "event booked over existing meetings",
                    extra={"event": "calendar.conflict", "conflict_count": len(conflicts)},
                )
            return event

    return await run_tool("create_event", _run)
