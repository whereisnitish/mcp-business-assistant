"""Lead persistence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select

from app.models.database.enums import LeadStatus
from app.models.database.lead import Lead
from app.repositories.base import BaseRepository, clamp_limit


class LeadRepository(BaseRepository[Lead]):
    model = Lead

    async def list_leads(
        self,
        *,
        status: LeadStatus | None = None,
        source: str | None = None,
        company: str | None = None,
        min_score: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Lead]:
        """Newest-first listing with optional filters."""
        stmt = select(Lead)
        if status is not None:
            stmt = stmt.where(Lead.status == status)
        if source:
            stmt = stmt.where(Lead.source == source)
        if company:
            stmt = stmt.where(Lead.company.ilike(f"%{company}%"))
        if min_score is not None:
            stmt = stmt.where(Lead.score >= min_score)
        stmt = (
            stmt.order_by(Lead.created_at.desc()).limit(clamp_limit(limit)).offset(max(0, offset))
        )
        return (await self.session.scalars(stmt)).all()

    async def search(self, query: str, *, limit: int | None = None) -> Sequence[Lead]:
        """Case-insensitive search across name, email, company and notes."""
        term = f"%{query.strip()}%"
        stmt = (
            select(Lead)
            .where(
                or_(
                    Lead.full_name.ilike(term),
                    Lead.email.ilike(term),
                    Lead.company.ilike(term),
                    Lead.notes.ilike(term),
                )
            )
            .order_by(Lead.created_at.desc())
            .limit(clamp_limit(limit))
        )
        return (await self.session.scalars(stmt)).all()

    async def get_by_email(self, email: str) -> Lead | None:
        stmt = select(Lead).where(Lead.email.ilike(email.strip())).order_by(Lead.created_at.desc())
        return (await self.session.scalars(stmt)).first()

    async def list_by_status(
        self, statuses: Sequence[LeadStatus], *, limit: int | None = None
    ) -> Sequence[Lead]:
        stmt = (
            select(Lead)
            .where(Lead.status.in_(list(statuses)))
            .order_by(Lead.score.desc(), Lead.created_at.desc())
            .limit(clamp_limit(limit))
        )
        return (await self.session.scalars(stmt)).all()

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
        owner_id: uuid.UUID | None = None,
    ) -> Lead:
        lead = Lead(
            full_name=full_name.strip(),
            email=email.strip().lower(),
            company=company,
            phone=phone,
            source=source,
            status=status,
            score=score,
            estimated_value=estimated_value,
            notes=notes,
            owner_id=owner_id,
        )
        return await self.add(lead)

    async def update_status(
        self, lead: Lead, status: LeadStatus, *, note: str | None = None
    ) -> Lead:
        """Transition a lead, appending an audit-friendly note to its history."""
        previous = lead.status
        lead.status = status
        if note:
            stamp = f"[{previous.value} -> {status.value}] {note}"
            lead.notes = f"{lead.notes}\n{stamp}" if lead.notes else stamp
        await self.session.flush()
        return lead
