"""Google Sheets spreadsheet integration -- INTENTIONALLY UNIMPLEMENTED.

This module documents the extension point rather than pretending to work. Every
method raises :class:`NotImplementedError` with the specific work required, so the
failure mode is a clear error at startup rather than silently wrong business data.

Why it is a stub while :mod:`app.integrations.crm.hubspot` is fully written: the
HubSpot integration needs only a bearer token and ``httpx``, both already present.
A working Sheets client needs the Google service-account OAuth flow and the
``google-api-python-client`` dependency tree, which is a poor trade for a
capability that the local provider already satisfies. Shipping a plausible-looking
but untested implementation would be worse than shipping an honest stub.

To implement:

1. ``pip install google-auth google-api-python-client``
2. Create a service account, download its JSON key, share the target spreadsheet
   with the service-account email.
3. Set ``SPREADSHEET_PROVIDER=google_sheets``, ``GOOGLE_SHEETS_SPREADSHEET_ID`` and
   ``GOOGLE_SERVICE_ACCOUNT_FILE``.
4. Implement the three methods below using ``spreadsheets.values.get`` /
   ``spreadsheets.values.append``. The Google client is synchronous, so wrap calls
   in ``asyncio.to_thread`` to avoid blocking the event loop.

The MCP tools, the agent and the prompts require **no changes** for this swap --
that separation is the point of the interface.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.config import Settings
from app.integrations.base import IntegrationNotConfiguredError
from app.integrations.spreadsheets.base import SpreadsheetInterface
from app.models.schemas.domain import SalesRecordDTO, SalesSummaryDTO, WeeklyReportDTO

_NOT_IMPLEMENTED = (
    "The Google Sheets provider is not implemented. "
    "Use SPREADSHEET_PROVIDER=local, or implement this class -- see the module docstring."
)


class GoogleSheetsIntegration(SpreadsheetInterface):
    """Placeholder implementation of :class:`SpreadsheetInterface`."""

    provider_name = "google_sheets"

    def __init__(self, settings: Settings) -> None:
        if not settings.google_sheets_spreadsheet_id or not settings.google_service_account_file:
            raise IntegrationNotConfiguredError(
                "google_sheets",
                "GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE must both be set",
            )
        self._spreadsheet_id = settings.google_sheets_spreadsheet_id
        self._credentials_file = settings.google_service_account_file

    async def health_check(self) -> bool:
        return False

    async def get_sales_summary(
        self, *, start: date, end: date, region: str | None = None
    ) -> SalesSummaryDTO:
        raise NotImplementedError(_NOT_IMPLEMENTED)

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
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def generate_weekly_report(self, *, week_of: date | None = None) -> WeeklyReportDTO:
        raise NotImplementedError(_NOT_IMPLEMENTED)
