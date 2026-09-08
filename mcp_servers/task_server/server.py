"""Task management MCP server.

Exposes follow-up work as tools. As with every server here, authorisation is not
performed at this layer -- see the module docstring of
:mod:`mcp_servers.crm_server.server` for why.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.integrations.factory import build_tasks
from app.models.database.enums import TaskPriority, TaskStatus
from app.models.schemas.domain import TaskDTO, TaskListDTO
from mcp_servers.common.runtime import build_server, ensure_utc, run_tool, tool_error, tool_session

logger = get_logger(__name__)

INSTRUCTIONS = """\
Task tools for tracking follow-up work.

Use these to create follow-up tasks (optionally linked to a CRM lead), list
outstanding work, find tasks that are past their due date, and mark tasks complete.
Task statuses are: open, in_progress, done, cancelled. Priorities are: low, medium,
high, urgent. Timestamps are ISO-8601; a value without a timezone is read as UTC.
"""

server = build_server("tasks", instructions=INSTRUCTIONS)


@server.tool(
    description=(
        "Create a follow-up task. Link it to a lead with lead_id when the task is "
        "about a specific customer, so it appears on that lead's record."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
)
async def create_task(
    title: Annotated[str, Field(min_length=1, max_length=300, description="Short task summary.")],
    description: Annotated[
        str | None, Field(max_length=5000, description="Fuller description of what needs doing.")
    ] = None,
    due_at: Annotated[
        datetime | None,
        Field(description="Deadline as an ISO-8601 timestamp, e.g. '2026-09-15T17:00:00Z'."),
    ] = None,
    priority: Annotated[TaskPriority, Field(description="Task priority.")] = TaskPriority.MEDIUM,
    lead_id: Annotated[
        str | None, Field(description="Identifier of a related CRM lead, if any.")
    ] = None,
) -> TaskDTO:
    async def _run() -> TaskDTO:
        async with tool_session() as session:
            return await build_tasks(session).create_task(
                title=title,
                description=description,
                due_at=ensure_utc(due_at) if due_at else None,
                priority=priority,
                lead_id=lead_id,
            )

    return await run_tool("create_task", _run)


@server.tool(
    description=(
        "List tasks ordered by due date, soonest first. By default only open and "
        "in-progress tasks are returned."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_tasks(
    status: Annotated[TaskStatus | None, Field(description="Only tasks in this status.")] = None,
    lead_id: Annotated[str | None, Field(description="Only tasks linked to this lead.")] = None,
    due_before: Annotated[
        datetime | None, Field(description="Only tasks due at or before this ISO-8601 timestamp.")
    ] = None,
    include_completed: Annotated[
        bool, Field(description="Include done and cancelled tasks.")
    ] = False,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum tasks to return.")] = 25,
) -> TaskListDTO:
    async def _run() -> TaskListDTO:
        async with tool_session() as session:
            tasks = await build_tasks(session).get_tasks(
                status=status,
                lead_id=lead_id,
                due_before=ensure_utc(due_before) if due_before else None,
                include_completed=include_completed,
                limit=limit,
            )
            return TaskListDTO(
                tasks=tasks,
                count=len(tasks),
                overdue_count=sum(1 for task in tasks if task.is_overdue),
            )

    return await run_tool("get_tasks", _run)


@server.tool(
    description=(
        "List open tasks whose due date has already passed, most overdue first. "
        "Use this for questions like 'what is overdue' or 'what have I missed'."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_overdue_tasks(
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum tasks to return.")] = 25,
) -> TaskListDTO:
    async def _run() -> TaskListDTO:
        async with tool_session() as session:
            tasks = await build_tasks(session).get_overdue_tasks(limit=limit)
            return TaskListDTO(tasks=tasks, count=len(tasks), overdue_count=len(tasks))

    return await run_tool("get_overdue_tasks", _run)


@server.tool(
    description="Mark a task as complete. Completing an already-completed task is harmless.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def complete_task(
    task_id: Annotated[str, Field(description="The task's unique identifier (UUID).")],
) -> TaskDTO:
    async def _run() -> TaskDTO:
        async with tool_session() as session:
            task = await build_tasks(session).complete_task(task_id)
            if task is None:
                raise tool_error(
                    f"No task exists with id {task_id!r}. Use get_tasks to find a valid id."
                )
            return task

    return await run_tool("complete_task", _run)
