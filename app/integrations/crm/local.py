"""PostgreSQL-backed CRM implementation.

This is a real implementation, not a stub: leads are persisted, queried and
transitioned in the application's own database. It is the default provider and the
one exercised by the test suite.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, safe_extra
from app.integrations.crm.base import CRMInterface
from app.models.database.enums import LeadStatus
from app.models.schemas.domain import LeadDTO
from app.repositories.lead_repository import LeadRepository

logger = get_logger(__name__)


class LocalCRMRepository(CRMInterface):
    """CRM backed by the local ``leads`` table."""

    provider_name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._leads = LeadRepository(session)

    async def health_check(self) -> bool:
        await self._leads.count()
        return True

    async def get_leads(
        self,
        *,
        status: LeadStatus | None = None,
        source: str | None = None,
        company: str | None = None,
        min_score: int | None = None,
        limit: int = 25,
    ) -> list[LeadDTO]:
        rows = await self._leads.list_leads(
            status=status, source=source, company=company, min_score=min_score, limit=limit
        )
        return [LeadDTO.model_validate(row) for row in rows]

    async def get_lead(self, lead_id: str) -> LeadDTO | None:
        row = await self._leads.get(lead_id)
        return LeadDTO.model_validate(row) if row else None

    async def search_leads(self, query: str, *, limit: int = 25) -> list[LeadDTO]:
        rows = await self._leads.search(query, limit=limit)
        return [LeadDTO.model_validate(row) for row in rows]

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
        existing = await self._leads.get_by_email(email)
        if existing is not None:
            # Deduplicate rather than creating a second record for the same person:
            # an agent re-running a create after a timeout is a realistic scenario,
            # and a duplicated pipeline entry is a real business cost.
            logger.info(
                "lead already exists, returning existing record",
                extra=safe_extra({"event": "crm.lead_deduplicated", "lead_id": str(existing.id)}),
            )
            return LeadDTO.model_validate(existing)

        row = await self._leads.create_lead(
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
        logger.info(
            "lead created", extra=safe_extra({"event": "crm.lead_created", "lead_id": str(row.id)})
        )
        return LeadDTO.model_validate(row)

    async def update_lead_status(
        self, lead_id: str, status: LeadStatus, *, note: str | None = None
    ) -> LeadDTO | None:
        row = await self._leads.get(lead_id)
        if row is None:
            return None
        updated = await self._leads.update_status(row, status, note=note)
        logger.info(
            "lead status updated",
            extra=safe_extra(
                {"event": "crm.lead_status_updated", "lead_id": str(row.id), "status": status.value}
            ),
        )
        return LeadDTO.model_validate(updated)
