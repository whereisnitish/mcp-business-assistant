"""User model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.database.enums import UserRole, str_enum_column

if TYPE_CHECKING:
    from app.models.database.approval import ApprovalRequest
    from app.models.database.conversation import Conversation


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated principal.

    Only the *hash* of an API key is stored; the plaintext key exists exactly once,
    at creation time, and is never persisted or logged.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[UserRole] = mapped_column(
        str_enum_column(UserRole, "user_role"), default=UserRole.OPERATOR, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    approval_requests: Mapped[list[ApprovalRequest]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="ApprovalRequest.user_id",
    )

    @property
    def can_approve(self) -> bool:
        """Viewers may never approve an action, only propose read-only ones."""
        return self.is_active and self.role in (UserRole.OPERATOR, UserRole.ADMIN)
