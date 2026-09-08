"""CRM interface.

The contract the CRM MCP tools are written against. Implementations must not leak
provider-specific types: everything crossing this boundary is a DTO from
:mod:`app.models.schemas.domain`.
"""

from __future__ import annotations

from abc import abstractmethod

from app.integrations.base import BusinessIntegration
from app.models.database.enums import LeadStatus
from app.models.schemas.domain import LeadDTO


class CRMInterface(BusinessIntegration):
    """Read/write access to a customer relationship management system."""

    @abstractmethod
    async def get_leads(
        self,
        *,
        status: LeadStatus | None = None,
        source: str | None = None,
        company: str | None = None,
        min_score: int | None = None,
        limit: int = 25,
    ) -> list[LeadDTO]:
        """List leads, newest first, honouring the supplied filters."""

    @abstractmethod
    async def get_lead(self, lead_id: str) -> LeadDTO | None:
        """Fetch a single lead, or ``None`` when it does not exist."""

    @abstractmethod
    async def search_leads(self, query: str, *, limit: int = 25) -> list[LeadDTO]:
        """Free-text search across name, email, company and notes."""

    @abstractmethod
    async def create_lead(
        self,
        *,
        full_name: str,
        email: str,
        company: str | None = None,
        phone: str | None = None,
        source: str | None = None,
        status: LeadStatus = LeadStatus.NEW,
        score: int = 0,
        estimated_value: int | None = None,
        notes: str | None = None,
    ) -> LeadDTO:
        """Create a lead and return it as persisted."""

    @abstractmethod
    async def update_lead_status(
        self, lead_id: str, status: LeadStatus, *, note: str | None = None
    ) -> LeadDTO | None:
        """Transition a lead's pipeline stage. ``None`` when the lead is unknown."""
