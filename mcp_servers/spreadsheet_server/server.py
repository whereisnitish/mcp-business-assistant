"""Spreadsheet / sales reporting MCP server.

Aggregation happens in SQL inside the repository layer, so these tools return a
handful of summary rows rather than thousands of raw records. That matters for an
agent: a tool that dumps a table into the context window is a tool that makes the
model slower, more expensive and more likely to miscount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.integrations.factory import build_spreadsheets
from app.models.schemas.domain import SalesRecordDTO, SalesSummaryDTO, WeeklyReportDTO
from mcp_servers.common.runtime import (
    build_server,
    resolve_period,
    run_tool,
    tool_error,
    tool_session,
)

logger = get_logger(__name__)

INSTRUCTIONS = """\
Sales data and reporting tools.

get_sales_summary aggregates revenue over a period with breakdowns by product and
region. generate_weekly_report produces a week-over-week comparison suitable for
sharing. add_sales_record appends a single sale; the line total is computed from
quantity and unit amount, so do not pass a total. Dates are ISO-8601 (YYYY-MM-DD);
when a period is omitted the last 30 days are used.
"""

server = build_server("spreadsheets", instructions=INSTRUCTIONS)


@server.tool(
    description=(
        "Summarise sales for a period, with totals and breakdowns by product and region. "
        "Defaults to the last 30 days when no dates are given."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_sales_summary(
    start_date: Annotated[
        date | None, Field(description="First day of the period, YYYY-MM-DD.")
    ] = None,
    end_date: Annotated[
        date | None, Field(description="Last day of the period, YYYY-MM-DD.")
    ] = None,
    region: Annotated[str | None, Field(description="Restrict the summary to one region.")] = None,
) -> SalesSummaryDTO:
    async def _run() -> SalesSummaryDTO:
        start, end = resolve_period(start_date, end_date, default_days=30)
        async with tool_session() as session:
            return await build_spreadsheets(session).get_sales_summary(
                start=start, end=end, region=region
            )

    return await run_tool("get_sales_summary", _run)


@server.tool(
    description=(
        "Append a single sale. The line total is calculated as quantity x unit_amount "
        "and must not be supplied."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
async def add_sales_record(
    record_date: Annotated[date, Field(description="Date of the sale, YYYY-MM-DD.")],
    customer_name: Annotated[
        str, Field(min_length=1, max_length=200, description="Customer name.")
    ],
    product: Annotated[
        str, Field(min_length=1, max_length=200, description="Product or service sold.")
    ],
    quantity: Annotated[int, Field(ge=1, le=100000, description="Units sold.")],
    unit_amount: Annotated[
        str,
        Field(
            description=(
                "Price per unit as a decimal string, e.g. '249.99'. A string is used "
                "rather than a number so the amount is not altered by float rounding."
            )
        ),
    ],
    currency: Annotated[
        str, Field(min_length=3, max_length=3, description="ISO-4217 code.")
    ] = "USD",
    region: Annotated[str | None, Field(max_length=100, description="Sales region.")] = None,
    sales_rep: Annotated[
        str | None, Field(max_length=200, description="Sales representative.")
    ] = None,
) -> SalesRecordDTO:
    async def _run() -> SalesRecordDTO:
        try:
            amount = Decimal(unit_amount.strip())
        except (InvalidOperation, AttributeError) as exc:
            raise tool_error(
                f"unit_amount {unit_amount!r} is not a valid "
                f"decimal amount; use a value like '249.99'."
            ) from exc
        if amount < 0:
            raise tool_error("unit_amount must not be negative.")

        async with tool_session() as session:
            return await build_spreadsheets(session).add_sales_record(
                record_date=record_date,
                customer_name=customer_name,
                product=product,
                quantity=quantity,
                unit_amount=amount,
                currency=currency,
                region=region,
                sales_rep=sales_rep,
            )

    return await run_tool("add_sales_record", _run)


@server.tool(
    description=(
        "Generate a weekly sales report comparing a week against the one before it. "
        "Defaults to the current week."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def generate_weekly_report(
    week_of: Annotated[
        date | None,
        Field(description="Any date within the target week, YYYY-MM-DD. Defaults to today."),
    ] = None,
) -> WeeklyReportDTO:
    async def _run() -> WeeklyReportDTO:
        async with tool_session() as session:
            return await build_spreadsheets(session).generate_weekly_report(week_of=week_of)

    return await run_tool("generate_weekly_report", _run)
