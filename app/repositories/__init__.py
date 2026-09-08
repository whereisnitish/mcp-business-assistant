"""Data access layer. All SQL lives here and nowhere else."""

from app.repositories.approval_repository import ApprovalRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.base import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, BaseRepository, clamp_limit
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.sales_repository import (
    GroupedTotal,
    SalesRepository,
    SalesTotals,
    week_bounds,
)
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ApprovalRepository",
    "AuditRepository",
    "BaseRepository",
    "CalendarRepository",
    "ConversationRepository",
    "GroupedTotal",
    "LeadRepository",
    "SalesRepository",
    "SalesTotals",
    "TaskRepository",
    "UserRepository",
    "clamp_limit",
    "week_bounds",
]
