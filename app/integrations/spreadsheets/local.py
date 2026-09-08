"""PostgreSQL-backed sales data and reporting."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, safe_extra
from app.integrations.spreadsheets.base import SpreadsheetInterface
from app.models.schemas.domain import (
    SalesBreakdownItemDTO,
    SalesRecordDTO,
    SalesSummaryDTO,
    WeeklyReportDTO,
)
from app.repositories.sales_repository import GroupedTotal, SalesRepository, week_bounds

logger = get_logger(__name__)


def _to_breakdown(items: list[GroupedTotal]) -> list[SalesBreakdownItemDTO]:
    return [
        SalesBreakdownItemDTO(
            key=item.key, revenue=item.revenue, record_count=item.record_count, units=item.units
        )
        for item in items
    ]


class LocalSpreadsheetRepository(SpreadsheetInterface):
    """Sales reporting over the local ``sales_records`` table."""

    provider_name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._sales = SalesRepository(session)

    async def health_check(self) -> bool:
        await self._sales.count()
        return True

    async def get_sales_summary(
        self, *, start: date, end: date, region: str | None = None
    ) -> SalesSummaryDTO:
        if end < start:
            start, end = end, start  # tolerate a reversed range from a model-proposed call

        totals = await self._sales.totals(start=start, end=end, region=region)
        by_product = await self._sales.grouped_totals(
            "product", start=start, end=end, region=region
        )
        by_region = await self._sales.grouped_totals("region", start=start, end=end, region=region)

        return SalesSummaryDTO(
            period_start=start,
            period_end=end,
            record_count=totals.record_count,
            total_revenue=totals.total_revenue,
            average_order_value=totals.average_order_value,
            units_sold=totals.units_sold,
            unique_customers=totals.unique_customers,
            by_product=_to_breakdown(by_product),
            by_region=_to_breakdown(by_region),
        )

    async def add_sales_record(
        self,
        *,
        record_date: date,
        customer_name: str,
        product: str,
        quantity: int,
        unit_amount: Decimal,
        currency: str = "USD",
        region: str | None = None,
        sales_rep: str | None = None,
    ) -> SalesRecordDTO:
        record = await self._sales.add_record(
            record_date=record_date,
            customer_name=customer_name,
            product=product,
            quantity=quantity,
            unit_amount=unit_amount,
            currency=currency,
            region=region,
            sales_rep=sales_rep,
        )
        logger.info(
            "sales record added",
            extra=safe_extra({"event": "sales.record_added", "record_id": str(record.id)}),
        )
        return SalesRecordDTO.model_validate(record)

    async def generate_weekly_report(self, *, week_of: date | None = None) -> WeeklyReportDTO:
        """Compare the target week against the one before it."""
        reference = week_of or datetime.now(UTC).date()
        week_start, week_end = week_bounds(reference)
        previous_start = week_start - timedelta(days=7)
        previous_end = week_start - timedelta(days=1)

        summary = await self.get_sales_summary(start=week_start, end=week_end)
        previous = await self._sales.totals(start=previous_start, end=previous_end)

        # Guard the division: a first trading week has no baseline, and reporting
        # "+infinity%" (or crashing) would be worse than reporting "no baseline".
        change_pct: float | None = None
        if previous.total_revenue > 0:
            delta = summary.total_revenue - previous.total_revenue
            change_pct = round(float(delta / previous.total_revenue) * 100, 2)

        top_products = summary.by_product[:5]
        headline = _build_headline(
            summary.total_revenue, summary.record_count, change_pct, top_products
        )

        return WeeklyReportDTO(
            week_start=week_start,
            week_end=week_end,
            summary=summary,
            previous_week_revenue=previous.total_revenue,
            revenue_change_pct=change_pct,
            top_products=top_products,
            headline=headline,
        )


def _build_headline(
    revenue: Decimal,
    record_count: int,
    change_pct: float | None,
    top_products: list[SalesBreakdownItemDTO],
) -> str:
    """Compose the report's one-line summary.

    Deliberately built in code rather than by the model: a revenue figure quoted in
    a business report must come from the database, not from a generated sentence.
    """
    if record_count == 0:
        return "No sales were recorded this week."

    parts = [f"{record_count} sales totalling {revenue:,.2f}"]
    if change_pct is not None:
        direction = "up" if change_pct >= 0 else "down"
        parts.append(f"{direction} {abs(change_pct):.1f}% week over week")
    else:
        parts.append("no prior week to compare against")
    if top_products:
        parts.append(f"led by {top_products[0].key}")
    return ", ".join(parts) + "."
