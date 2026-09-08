"""Task persistence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.database.enums import TaskPriority, TaskStatus
from app.models.database.task import Task
from app.repositories.base import BaseRepository, clamp_limit

#: Statuses that still count as outstanding work.
OPEN_STATUSES = (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)


class TaskRepository(BaseRepository[Task]):
    model = Task

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assignee_id: uuid.UUID | None = None,
        lead_id: uuid.UUID | None = None,
        due_before: datetime | None = None,
        include_completed: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Task]:
        """List tasks ordered by due date, with undated tasks last."""
        stmt = select(Task)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        elif not include_completed:
            stmt = stmt.where(Task.status.in_(OPEN_STATUSES))
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        if lead_id is not None:
            stmt = stmt.where(Task.lead_id == lead_id)
        if due_before is not None:
            stmt = stmt.where(Task.due_at <= due_before)
        stmt = (
            stmt.order_by(Task.due_at.is_(None), Task.due_at.asc(), Task.created_at.desc())
            .limit(clamp_limit(limit))
            .offset(max(0, offset))
        )
        return (await self.session.scalars(stmt)).all()

    async def list_overdue(
        self,
        *,
        now: datetime | None = None,
        assignee_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> Sequence[Task]:
        """Open tasks whose due date has passed, most overdue first."""
        cutoff = now or datetime.now(UTC)
        stmt = select(Task).where(
            Task.status.in_(OPEN_STATUSES),
            Task.due_at.is_not(None),
            Task.due_at < cutoff,
        )
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        stmt = stmt.order_by(Task.due_at.asc()).limit(clamp_limit(limit))
        return (await self.session.scalars(stmt)).all()

    async def create_task(
        self,
        *,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: TaskPriority = TaskPriority.MEDIUM,
        lead_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
    ) -> Task:
        task = Task(
            title=title.strip(),
            description=description,
            due_at=due_at,
            priority=priority,
            lead_id=lead_id,
            assignee_id=assignee_id,
            status=TaskStatus.OPEN,
        )
        return await self.add(task)

    async def complete(self, task: Task, *, completed_at: datetime | None = None) -> Task:
        """Mark a task done. Idempotent -- re-completing keeps the original timestamp."""
        if task.status != TaskStatus.DONE:
            task.status = TaskStatus.DONE
            task.completed_at = completed_at or datetime.now(UTC)
            await self.session.flush()
        return task

    async def count_open(self, *, assignee_id: uuid.UUID | None = None) -> int:
        stmt = select(Task).where(Task.status.in_(OPEN_STATUSES))
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        return await self.count(stmt)
