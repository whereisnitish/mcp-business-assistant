"""CRM lead model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONColumn
from app.models.database.enums import LeadStatus, str_enum_column

if TYPE_CHECKING:
    from app.models.database.task import Task


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A sales lead.

    ``external_id`` and ``source_system`` exist so that rows created locally can be
    reconciled with a real CRM (HubSpot, Salesforce) once a live integration is
    swapped in behind ``CRMInterface`` -- the MCP tool contract does not change.
    """

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_status_created", "status", "created_at"),
        Index("ix_leads_source_external", "source_system", "external_id", unique=True),
    )

    full_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    company: Mapped[str | None] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str | None] = mapped_column(String(100), index=True)
    status: Mapped[LeadStatus] = mapped_column(
        str_enum_column(LeadStatus, "lead_status"),
        default=LeadStatus.NEW,
        nullable=False,
        index=True,
    )
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_value: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Reconciliation with an external system of record.
    source_system: Mapped[str] = mapped_column(String(50), default="local", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    tasks: Mapped[list[Task]] = relationship(back_populates="lead", lazy="selectin")

    @property
    def is_qualified(self) -> bool:
        return self.status in (LeadStatus.QUALIFIED, LeadStatus.WON)
