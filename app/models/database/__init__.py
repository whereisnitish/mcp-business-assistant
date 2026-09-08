"""SQLAlchemy ORM models.

Importing this package registers every mapper, which is required before
``Base.metadata`` is complete (schema creation, Alembic autogenerate).
"""

from app.db.base import Base
from app.models.database.approval import TERMINAL_APPROVAL_STATUSES, ApprovalRequest
from app.models.database.audit import AuditLog
from app.models.database.calendar import CalendarEvent
from app.models.database.conversation import Conversation, Message
from app.models.database.enums import (
    PERMISSION_ORDER,
    ApprovalStatus,
    AuditEventType,
    AuditOutcome,
    ConversationStatus,
    EmailStatus,
    LeadStatus,
    MessageRole,
    PermissionLevel,
    TaskPriority,
    TaskStatus,
    UserRole,
)
from app.models.database.lead import Lead
from app.models.database.sales import SalesRecord
from app.models.database.task import Task
from app.models.database.user import User

__all__ = [
    "PERMISSION_ORDER",
    "TERMINAL_APPROVAL_STATUSES",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuditEventType",
    "AuditLog",
    "AuditOutcome",
    "Base",
    "CalendarEvent",
    "Conversation",
    "ConversationStatus",
    "EmailStatus",
    "Lead",
    "LeadStatus",
    "Message",
    "MessageRole",
    "PermissionLevel",
    "SalesRecord",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "User",
    "UserRole",
]
