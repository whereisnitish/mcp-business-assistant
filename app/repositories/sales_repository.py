"""Sales persistence and aggregation.

Aggregations are pushed into SQL (``SUM``/``COUNT``/``AVG`` with ``GROUP BY``)
rather than loaded into Python. Beyond being faster, it keeps the row volume out of
the agent's context: a weekly report over 50,000 sales returns a handful of
aggregate rows, not 50,000 records for the model to summarise.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select

from app.models.database.sales import SalesRecord
from app.repositories.base import BaseRepository, clamp_limit


class SalesTotals(NamedTuple):
    """Headline figures for a period."""

    record_count: int
    total_revenue: Decimal
    average_order_value: Decimal
    units_sold: int
    unique_customers: int


class GroupedTotal(NamedTuple):
    """One row of a grouped breakdown (by product, region or day)."""

    key: str
    revenue: Decimal
    record_count: int
    units: int


def week_bounds(reference: date) -> tuple[date, date]:
    """Return the Monday..Sunday range containing *reference*."""
    start = reference - timedelta(days=reference.weekday())
    return start, start + timedelta(days=6)


class SalesRepository(BaseRepository[SalesRecord]):
    model = SalesRecord

    async def add_record(
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
        lead_id: uuid.UUID | None = None,
    ) -> SalesRecord:
        record = SalesRecord(
            record_date=record_date,
            customer_name=customer_name.strip(),
            product=product.strip(),
            quantity=quantity,
            unit_amount=unit_amount,
            total_amount=SalesRecord.compute_total(unit_amount, quantity),
            currency=currency.upper(),
            region=region,
            sales_rep=sales_rep,
            lead_id=lead_id,
        )
        return await self.add(record)

    async def list_records(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        region: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SalesRecord]:
        stmt = select(SalesRecord)
        if start is not None:
            stmt = stmt.where(SalesRecord.record_date >= start)
        if end is not None:
            stmt = stmt.where(SalesRecord.record_date <= end)
        if region:
            stmt = stmt.where(SalesRecord.region == region)
        stmt = stmt.order_by(SalesRecord.record_date.desc()).limit(clamp_limit(limit))
        return (await self.session.scalars(stmt)).all()

    async def totals(
        self, *, start: date | None = None, end: date | None = None, region: str | None = None
    ) -> SalesTotals:
        """Headline aggregates for a period."""
        stmt = select(
            func.count(SalesRecord.id),
            func.coalesce(func.sum(SalesRecord.total_amount), 0),
            func.coalesce(func.sum(SalesRecord.quantity), 0),
            func.count(func.distinct(SalesRecord.customer_name)),
        )
        stmt = self._apply_filters(stmt, start=start, end=end, region=region)
        count, revenue, units, customers = (await self.session.execute(stmt)).one()

        total_revenue = Decimal(revenue or 0).quantize(Decimal("0.01"))
        average = (total_revenue / count).quantize(Decimal("0.01")) if count else Decimal("0.00")
        return SalesTotals(
            record_count=int(count),
            total_revenue=total_revenue,
            average_order_value=average,
            units_sold=int(units or 0),
            unique_customers=int(customers or 0),
        )

    async def grouped_totals(
        self,
        group_by: str,
        *,
        start: date | None = None,
        end: date | None = None,
        region: str | None = None,
        limit: int = 20,
    ) -> list[GroupedTotal]:
        """Revenue breakdown grouped by ``product``, ``region``, ``day`` or ``sales_rep``.

        The grouping column is resolved from a fixed allow-list, never interpolated
        from the caller's string -- this path is reachable from model-proposed tool
        arguments.
        """
        columns = {
            "product": SalesRecord.product,
            "region": SalesRecord.region,
            "day": SalesRecord.record_date,
            "sales_rep": SalesRecord.sales_rep,
            "customer": SalesRecord.customer_name,
        }
        column = columns.get(group_by)
        if column is None:
            raise ValueError(
                f"Unsupported grouping {group_by!r}. Expected one of: {', '.join(sorted(columns))}."
            )

        stmt = select(
            column,
            func.coalesce(func.sum(SalesRecord.total_amount), 0),
            func.count(SalesRecord.id),
            func.coalesce(func.sum(SalesRecord.quantity), 0),
        )
        stmt = self._apply_filters(stmt, start=start, end=end, region=region)
        stmt = (
            stmt.group_by(column)
            .order_by(func.coalesce(func.sum(SalesRecord.total_amount), 0).desc())
            .limit(clamp_limit(limit))
        )

        return [
            GroupedTotal(
                key=str(key) if key is not None else "unknown",
                revenue=Decimal(revenue or 0).quantize(Decimal("0.01")),
                record_count=int(count),
                units=int(units or 0),
            )
            for key, revenue, count, units in (await self.session.execute(stmt)).all()
        ]

    @staticmethod
    def _apply_filters(stmt, *, start: date | None, end: date | None, region: str | None):  # type: ignore[no-untyped-def]
        if start is not None:
            stmt = stmt.where(SalesRecord.record_date >= start)
        if end is not None:
            stmt = stmt.where(SalesRecord.record_date <= end)
        if region:
            stmt = stmt.where(SalesRecord.region == region)
        return stmt
