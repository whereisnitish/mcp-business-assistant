"""Business integrations.

Each capability is an abstract interface with one or more concrete providers.
Provider selection happens exclusively in :mod:`app.integrations.factory`.
"""

from app.integrations.base import (
    BusinessIntegration,
    IntegrationError,
    IntegrationNotConfiguredError,
)
from app.integrations.calendar.base import CalendarInterface
from app.integrations.crm.base import CRMInterface
from app.integrations.email.base import MAX_RECIPIENTS, EmailInterface
from app.integrations.factory import (
    build_calendar,
    build_crm,
    build_email,
    build_spreadsheets,
    build_tasks,
)
from app.integrations.spreadsheets.base import SpreadsheetInterface
from app.integrations.tasks.base import TaskInterface

__all__ = [
    "MAX_RECIPIENTS",
    "BusinessIntegration",
    "CRMInterface",
    "CalendarInterface",
    "EmailInterface",
    "IntegrationError",
    "IntegrationNotConfiguredError",
    "SpreadsheetInterface",
    "TaskInterface",
    "build_calendar",
    "build_crm",
    "build_email",
    "build_spreadsheets",
    "build_tasks",
]
