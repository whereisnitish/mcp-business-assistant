"""Task management model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import UTCDateTime
from app.models.database.enums import TaskPriority, TaskStatus, str_enum_column

if TYPE_CHECKING:
    from app.models.database.lead import Lead


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A follow-up task, optionally attached to a lead."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_due", "status", "due_at"),
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        str_enum_column(TaskStatus, "task_status"),
        default=TaskStatus.OPEN,
        nullable=False,
        index=True,
    )
    priority: Mapped[TaskPriority] = mapped_column(
        str_enum_column(TaskPriority, "task_priority"), default=TaskPriority.MEDIUM, nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    lead: Mapped[Lead | None] = relationship(back_populates="tasks", lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)

    def is_overdue(self, now: datetime | None = None) -> bool:
        """A task is overdue when it is still open and its due date has passed.

        Timestamps read back from SQLite are naive; they are treated as UTC so the
        comparison is well-defined on every backend.
        """
        if self.due_at is None or not self.is_open:
            return False
        due = self.due_at if self.due_at.tzinfo else self.due_at.replace(tzinfo=UTC)
        return due < (now or datetime.now(UTC))
