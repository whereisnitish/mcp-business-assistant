"""Integration factory.

The **only** place in the codebase that decides which concrete provider backs a
business capability. Every consumer -- above all the MCP tools -- asks for an
interface and receives whatever the configuration selected. Grepping for
``HubSpotCRMIntegration`` or ``SMTPEmailProvider`` outside this module should
return nothing; that is the invariant which keeps provider choice from leaking
into tool logic.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.integrations.calendar.base import CalendarInterface
from app.integrations.calendar.local import LocalCalendarRepository
from app.integrations.crm.base import CRMInterface
from app.integrations.crm.hubspot import HubSpotCRMIntegration
from app.integrations.crm.local import LocalCRMRepository
from app.integrations.email.base import EmailInterface
from app.integrations.email.mock import MockEmailProvider
from app.integrations.email.smtp import SMTPEmailProvider
from app.integrations.spreadsheets.base import SpreadsheetInterface
from app.integrations.spreadsheets.google_sheets import GoogleSheetsIntegration
from app.integrations.spreadsheets.local import LocalSpreadsheetRepository
from app.integrations.tasks.base import TaskInterface
from app.integrations.tasks.local import LocalTaskRepository


def build_crm(session: AsyncSession, settings: Settings | None = None) -> CRMInterface:
    """Return the configured CRM provider.

    ``session`` is required even for remote providers so the signature is uniform;
    remote implementations simply ignore it.
    """
    settings = settings or get_settings()
    if settings.crm_provider == "hubspot":
        return HubSpotCRMIntegration(settings)
    return LocalCRMRepository(session)


def build_tasks(session: AsyncSession, settings: Settings | None = None) -> TaskInterface:
    """Return the configured task provider (currently local only)."""
    return LocalTaskRepository(session)


def build_spreadsheets(
    session: AsyncSession, settings: Settings | None = None
) -> SpreadsheetInterface:
    """Return the configured spreadsheet/reporting provider."""
    settings = settings or get_settings()
    if settings.spreadsheet_provider == "google_sheets":
        return GoogleSheetsIntegration(settings)
    return LocalSpreadsheetRepository(session)


def build_email(settings: Settings | None = None) -> EmailInterface:
    """Return the configured email provider.

    Takes no session: email delivery is not a database operation, and pretending
    otherwise would imply a transactional guarantee that SMTP cannot offer.
    """
    settings = settings or get_settings()
    if settings.email_provider == "smtp":
        return SMTPEmailProvider(settings)
    return MockEmailProvider(settings.email_outbox_dir, settings.email_from_address)


def build_calendar(session: AsyncSession, settings: Settings | None = None) -> CalendarInterface:
    """Return the configured calendar provider (currently local only)."""
    return LocalCalendarRepository(session)
