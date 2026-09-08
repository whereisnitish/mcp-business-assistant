"""CRM MCP server.

Exposes customer-relationship tools over the Model Context Protocol. Every tool is
a thin adapter: it validates and coerces the arguments the agent proposed, delegates
to :class:`~app.integrations.crm.base.CRMInterface`, and returns a DTO. No SQL, no
provider-specific logic and -- importantly -- **no authorisation logic** lives here.

That last point is deliberate. An MCP server is a capability provider, not a policy
decision point: it is reachable by any client that can open its transport. The
decision about whether a given tool may run for a given user is made in the API
process by :mod:`app.security.policy`, before the call is ever dispatched. Putting
the check here instead would place it under the control of whatever process happens
to host the server.

The ``annotations`` on each tool (``read_only_hint``, ``destructive_hint``) are
advisory metadata for display and for clients that have no policy of their own.
This application does not use them to authorise anything.
"""

from __future__ import annotations

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.integrations.factory import build_crm
from app.models.database.enums import LeadStatus
from app.models.schemas.domain import LeadDTO, LeadListDTO
from mcp_servers.common.runtime import build_server, run_tool, tool_error, tool_session

logger = get_logger(__name__)

INSTRUCTIONS = """\
CRM tools for working with sales leads.

Use these to look up leads, search the pipeline by free text, add new leads and move
leads between pipeline stages. Lead statuses are: new, contacted, qualified,
unqualified, won, lost. Prefer search_leads when the user names a person or company
in prose, and get_leads when they describe a filter such as "qualified leads".
"""

server = build_server("crm", instructions=INSTRUCTIONS)


@server.tool(
    description=(
        "List CRM leads, newest first, with optional filters. "
        "Use this for questions like 'show me the latest leads' or 'which leads are qualified'."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_leads(
    status: Annotated[
        LeadStatus | None,
        Field(description="Only return leads in this pipeline stage."),
    ] = None,
    source: Annotated[
        str | None, Field(description="Only return leads from this source, e.g. 'webinar'.")
    ] = None,
    company: Annotated[
        str | None, Field(description="Case-insensitive partial match on company name.")
    ] = None,
    min_score: Annotated[
        int | None, Field(ge=0, le=100, description="Only return leads scoring at least this.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum leads to return.")] = 25,
) -> LeadListDTO:
    async def _run() -> LeadListDTO:
        async with tool_session() as session:
            leads = await build_crm(session).get_leads(
                status=status, source=source, company=company, min_score=min_score, limit=limit
            )
            return LeadListDTO(
                leads=leads,
                count=len(leads),
                filters_applied={
                    key: value
                    for key, value in {
                        "status": status.value if status else None,
                        "source": source,
                        "company": company,
                        "min_score": min_score,
                    }.items()
                    if value is not None
                },
            )

    return await run_tool("get_leads", _run)


@server.tool(
    description="Fetch a single lead by its unique identifier.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_lead(
    lead_id: Annotated[str, Field(description="The lead's unique identifier (UUID).")],
) -> LeadDTO:
    async def _run() -> LeadDTO:
        async with tool_session() as session:
            lead = await build_crm(session).get_lead(lead_id)
            if lead is None:
                # Anticipated: the agent may have guessed or reused a stale id. The
                # message tells it what to do next instead of masking the failure.
                raise tool_error(
                    f"No lead exists with id {lead_id!r}. Use "
                    f"search_leads or get_leads to find a valid id."
                )
            return lead

    return await run_tool("get_lead", _run)


@server.tool(
    description=(
        "Search leads by free text across name, email, company and notes. "
        "Use this when the user refers to a person or company by name."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def search_leads(
    query: Annotated[str, Field(min_length=1, description="Text to search for.")],
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum leads to return.")] = 25,
) -> LeadListDTO:
    async def _run() -> LeadListDTO:
        async with tool_session() as session:
            leads = await build_crm(session).search_leads(query, limit=limit)
            return LeadListDTO(leads=leads, count=len(leads), filters_applied={"query": query})

    return await run_tool("search_leads", _run)


@server.tool(
    description=(
        "Create a new lead in the CRM. If a lead with the same email already exists, "
        "the existing lead is returned instead of creating a duplicate."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def create_lead(
    full_name: Annotated[
        str, Field(min_length=1, max_length=200, description="Contact's full name.")
    ],
    email: Annotated[
        str, Field(min_length=3, max_length=320, description="Contact's email address.")
    ],
    company: Annotated[str | None, Field(max_length=200, description="Company name.")] = None,
    phone: Annotated[str | None, Field(max_length=50, description="Contact phone number.")] = None,
    source: Annotated[
        str | None, Field(max_length=100, description="Where the lead came from, e.g. 'referral'.")
    ] = None,
    status: Annotated[LeadStatus, Field(description="Initial pipeline stage.")] = LeadStatus.NEW,
    score: Annotated[
        int, Field(ge=0, le=100, description="Lead score, 0 (cold) to 100 (hot).")
    ] = 0,
    estimated_value: Annotated[
        int | None, Field(ge=0, description="Estimated deal value in whole currency units.")
    ] = None,
    notes: Annotated[str | None, Field(max_length=5000, description="Free-form notes.")] = None,
) -> LeadDTO:
    async def _run() -> LeadDTO:
        async with tool_session() as session:
            return await build_crm(session).create_lead(
                full_name=full_name,
                email=email,
                company=company,
                phone=phone,
                source=source,
                status=status,
                score=score,
                estimated_value=estimated_value,
                notes=notes,
            )

    return await run_tool("create_lead", _run)


@server.tool(
    description=(
        "Move a lead to a different pipeline stage, optionally recording why. "
        "Use this to qualify, disqualify or close a lead."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def update_lead_status(
    lead_id: Annotated[str, Field(description="The lead's unique identifier (UUID).")],
    status: Annotated[LeadStatus, Field(description="The new pipeline stage.")],
    note: Annotated[
        str | None,
        Field(max_length=2000, description="Why the status changed; appended to the notes."),
    ] = None,
) -> LeadDTO:
    async def _run() -> LeadDTO:
        async with tool_session() as session:
            lead = await build_crm(session).update_lead_status(lead_id, status, note=note)
            if lead is None:
                raise tool_error(f"No lead exists with id {lead_id!r}; nothing was changed.")
            return lead

    return await run_tool("update_lead_status", _run)
