"""Domain transfer objects shared by the integration layer and the MCP tools.

These Pydantic models are the *contract* of the business layer. They serve three
purposes at once:

1. They are the return type of every ``CRMInterface`` / ``TaskInterface`` / ... method,
   so a local PostgreSQL implementation and a real SaaS integration are forced to
   produce the same shape.
2. The MCP SDK derives each tool's JSON **output schema** from them automatically,
   so a client knows the shape of a result before calling.
3. They are what the agent sees. Identifiers are strings rather than ``UUID``
   objects and money is rendered as a fixed-precision string, because both survive
   a JSON round-trip through a language model without being mangled.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, field_serializer

from app.models.database.enums import EmailStatus, LeadStatus, TaskPriority, TaskStatus


def _coerce_identifier(value: object) -> object:
    """Render an identifier as a string during validation.

    ORM rows carry ``uuid.UUID`` primary keys while these DTOs expose plain strings.
    A serializer alone is not enough -- it runs on the way *out*, so validation of a
    ``UUID`` against a ``str`` field would fail first. Coercing here means the same
    DTO validates cleanly from an ORM row, from a remote API payload and from JSON.
    """
    return str(value) if isinstance(value, UUID) else value


#: A required entity identifier, accepted as ``UUID`` or ``str``, exposed as ``str``.
EntityId = Annotated[str, BeforeValidator(_coerce_identifier)]
#: The optional variant, for nullable foreign keys.
OptionalEntityId = Annotated[str | None, BeforeValidator(_coerce_identifier)]


class DomainModel(BaseModel):
    """Base for every DTO: immutable, populated straight from ORM objects."""

    model_config = ConfigDict(from_attributes=True, frozen=True, use_enum_values=False)


# --------------------------------------------------------------------------- #
# CRM
# --------------------------------------------------------------------------- #
class LeadDTO(DomainModel):
    """A CRM lead as exposed to the agent."""

    id: EntityId = Field(description="Unique lead identifier.")
    full_name: str = Field(description="Contact's full name.")
    email: str = Field(description="Contact's email address.")
    company: str | None = Field(default=None, description="Company the lead belongs to.")
    phone: str | None = None
    source: str | None = Field(
        default=None, description="Where the lead came from, e.g. 'webinar'."
    )
    status: LeadStatus = Field(description="Current pipeline stage.")
    score: int = Field(default=0, description="Lead score from 0 (cold) to 100 (hot).")
    estimated_value: int | None = Field(default=None, description="Estimated deal value.")
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class LeadListDTO(DomainModel):
    """A page of leads plus the filters that produced it."""

    leads: list[LeadDTO]
    count: int = Field(description="Number of leads in this response.")
    filters_applied: dict[str, Any] = Field(
        default_factory=dict,
        description="Echo of the filters used, so the agent can explain the result.",
    )


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
class TaskDTO(DomainModel):
    id: EntityId
    title: str
    description: str | None = None
    status: TaskStatus
    priority: TaskPriority
    due_at: datetime | None = Field(default=None, description="Deadline, ISO-8601 with timezone.")
    completed_at: datetime | None = None
    lead_id: OptionalEntityId = Field(default=None, description="Related lead, if any.")
    is_overdue: bool = Field(default=False, description="True when open and past its due date.")
    created_at: datetime


class TaskListDTO(DomainModel):
    tasks: list[TaskDTO]
    count: int
    overdue_count: int = Field(default=0, description="How many of the returned tasks are overdue.")


# --------------------------------------------------------------------------- #
# Sales / spreadsheets
# --------------------------------------------------------------------------- #
class SalesRecordDTO(DomainModel):
    id: EntityId
    record_date: date
    customer_name: str
    product: str
    quantity: int
    unit_amount: Decimal
    total_amount: Decimal
    currency: str = "USD"
    region: str | None = None
    sales_rep: str | None = None

    @field_serializer("unit_amount", "total_amount")
    def _serialize_money(self, value: Decimal) -> str:
        """Serialise money as a fixed-precision string.

        A float would reintroduce binary rounding error the ``Numeric`` column
        exists to avoid, and would let a model read back 249.99999999.
        """
        return f"{value:.2f}"


class SalesBreakdownItemDTO(DomainModel):
    key: str = Field(description="Group label, e.g. a product or region name.")
    revenue: Decimal
    record_count: int
    units: int

    @field_serializer("revenue")
    def _serialize_money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class SalesSummaryDTO(DomainModel):
    """Aggregated sales figures for a period."""

    period_start: date
    period_end: date
    currency: str = "USD"
    record_count: int
    total_revenue: Decimal
    average_order_value: Decimal
    units_sold: int
    unique_customers: int
    by_product: list[SalesBreakdownItemDTO] = Field(default_factory=list)
    by_region: list[SalesBreakdownItemDTO] = Field(default_factory=list)

    @field_serializer("total_revenue", "average_order_value")
    def _serialize_money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class WeeklyReportDTO(DomainModel):
    """A formatted weekly sales report, ready to paste into a document or email."""

    week_start: date
    week_end: date
    summary: SalesSummaryDTO
    previous_week_revenue: Decimal
    revenue_change_pct: float | None = Field(
        default=None,
        description="Week-over-week change; null when the previous week had no revenue.",
    )
    top_products: list[SalesBreakdownItemDTO] = Field(default_factory=list)
    headline: str = Field(description="One-line plain-language summary.")

    @field_serializer("previous_week_revenue")
    def _serialize_money(self, value: Decimal) -> str:
        return f"{value:.2f}"


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
class CalendarEventDTO(DomainModel):
    id: EntityId
    title: str
    description: str | None = None
    location: str | None = None
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    attendees: list[str] = Field(default_factory=list)
    duration_minutes: int = 0
    lead_id: OptionalEntityId = None


class CalendarEventListDTO(DomainModel):
    events: list[CalendarEventDTO]
    count: int
    range_start: datetime
    range_end: datetime


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
class EmailDraftDTO(DomainModel):
    """A prepared email that has NOT been sent.

    Drafting is a READ-level operation precisely so the agent can compose freely;
    sending is HIGH_RISK and gated behind human approval.
    """

    to: list[EmailStr]
    subject: str
    body: str
    cc: list[EmailStr] = Field(default_factory=list)
    reply_to: str | None = None
    recipient_count: int = 0
    preview: str = Field(default="", description="First ~200 characters of the body.")


class EmailSendResultDTO(DomainModel):
    status: EmailStatus
    message_id: str | None = None
    to: list[str]
    subject: str
    provider: str = Field(description="Which provider handled the send, e.g. 'mock' or 'smtp'.")
    sent_at: datetime | None = None
    detail: str | None = None
