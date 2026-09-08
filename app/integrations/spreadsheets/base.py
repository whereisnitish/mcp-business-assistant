"""Spreadsheet / sales-reporting interface."""

from __future__ import annotations

from abc import abstractmethod
from datetime import date
from decimal import Decimal

from app.integrations.base import BusinessIntegration
from app.models.schemas.domain import SalesRecordDTO, SalesSummaryDTO, WeeklyReportDTO


class SpreadsheetInterface(BusinessIntegration):
    """Sales data storage and reporting.

    Named for the business concept rather than the storage medium: the default
    implementation is a PostgreSQL table, while a Google Sheets implementation
    satisfies the same contract.
    """

    @abstractmethod
    async def get_sales_summary(
        self, *, start: date, end: date, region: str | None = None
    ) -> SalesSummaryDTO:
        """Aggregate revenue for a period, broken down by product and region."""

    @abstractmethod
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
        """Append a sale and return the stored row, including its computed total."""

    @abstractmethod
    async def generate_weekly_report(self, *, week_of: date | None = None) -> WeeklyReportDTO:
        """Build a week-over-week report for the week containing ``week_of``."""
