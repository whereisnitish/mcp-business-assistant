"""PostgreSQL-backed task management."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, safe_extra
from app.integrations.tasks.base import TaskInterface
from app.models.database.enums import TaskPriority, TaskStatus
from app.models.database.task import Task
from app.models.schemas.domain import TaskDTO
from app.repositories.task_repository import TaskRepository

logger = get_logger(__name__)


def to_task_dto(task: Task) -> TaskDTO:
    """Map an ORM task to its DTO.

    ``is_overdue`` is computed here rather than stored: a persisted flag would go
    stale the moment a deadline passes with no write to the row.
    """
    return TaskDTO(
        id=str(task.id),
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        due_at=task.due_at,
        completed_at=task.completed_at,
        lead_id=str(task.lead_id) if task.lead_id else None,
        is_overdue=task.is_overdue(),
        created_at=task.created_at,
    )


class LocalTaskRepository(TaskInterface):
    """Task management backed by the local ``tasks`` table."""

    provider_name = "local"

    def __init__(self, session: AsyncSession) -> None:
        self._tasks = TaskRepository(session)

    async def health_check(self) -> bool:
        await self._tasks.count()
        return True

    async def create_task(
        self,
        *,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: TaskPriority = TaskPriority.MEDIUM,
        lead_id: str | None = None,
    ) -> TaskDTO:
        task = await self._tasks.create_task(
            title=title,
            description=description,
            due_at=due_at,
            priority=priority,
            lead_id=_maybe_uuid(lead_id),
        )
        logger.info(
            "task created", extra=safe_extra({"event": "tasks.created", "task_id": str(task.id)})
        )
        return to_task_dto(task)

    async def get_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        lead_id: str | None = None,
        due_before: datetime | None = None,
        include_completed: bool = False,
        limit: int = 25,
    ) -> list[TaskDTO]:
        rows = await self._tasks.list_tasks(
            status=status,
            lead_id=_maybe_uuid(lead_id),
            due_before=due_before,
            include_completed=include_completed,
            limit=limit,
        )
        return [to_task_dto(row) for row in rows]

    async def get_overdue_tasks(self, *, limit: int = 25) -> list[TaskDTO]:
        return [to_task_dto(row) for row in await self._tasks.list_overdue(limit=limit)]

    async def complete_task(self, task_id: str) -> TaskDTO | None:
        task = await self._tasks.get(task_id)
        if task is None:
            return None
        completed = await self._tasks.complete(task)
        logger.info(
            "task completed",
            extra=safe_extra({"event": "tasks.completed", "task_id": str(task.id)}),
        )
        return to_task_dto(completed)


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    """Coerce an optional tool-supplied id, treating malformed input as absent."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
