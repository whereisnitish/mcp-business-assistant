"""Sales record model backing the spreadsheet/reporting tools."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import MoneyColumn


class SalesRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single booked sale.

    This is the local stand-in for a "sales spreadsheet". Aggregations are computed
    in SQL rather than in Python so that a weekly report stays correct (and fast)
    as the table grows -- see :class:`~app.repositories.sales_repository.SalesRepository`.
    """

    __tablename__ = "sales_records"
    __table_args__ = (
        Index("ix_sales_records_date_region", "record_date", "region"),
        Index("ix_sales_records_product_date", "product", "record_date"),
    )

    record_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    product: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_amount: Mapped[Decimal] = mapped_column(MoneyColumn, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(MoneyColumn, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    region: Mapped[str | None] = mapped_column(String(100), index=True)
    sales_rep: Mapped[str | None] = mapped_column(String(200))

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )

    @staticmethod
    def compute_total(unit_amount: Decimal, quantity: int) -> Decimal:
        """Derive the line total. Kept as a single definition so the tool layer,
        the repository and any future import path cannot disagree."""
        return (Decimal(unit_amount) * Decimal(quantity)).quantize(Decimal("0.01"))
