"""Task management interface."""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from app.integrations.base import BusinessIntegration
from app.models.database.enums import TaskPriority, TaskStatus
from app.models.schemas.domain import TaskDTO


class TaskInterface(BusinessIntegration):
    """Create and track follow-up work.

    A real deployment would put Jira, Asana or Linear behind this interface.
    """

    @abstractmethod
    async def create_task(
        self,
        *,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: TaskPriority = TaskPriority.MEDIUM,
        lead_id: str | None = None,
    ) -> TaskDTO:
        """Create a task and return it as persisted."""

    @abstractmethod
    async def get_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        lead_id: str | None = None,
        due_before: datetime | None = None,
        include_completed: bool = False,
        limit: int = 25,
    ) -> list[TaskDTO]:
        """List tasks ordered by due date."""

    @abstractmethod
    async def get_overdue_tasks(self, *, limit: int = 25) -> list[TaskDTO]:
        """Open tasks whose due date has passed, most overdue first."""

    @abstractmethod
    async def complete_task(self, task_id: str) -> TaskDTO | None:
        """Mark a task done. ``None`` when the task is unknown."""
