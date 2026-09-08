"""User persistence."""

from __future__ import annotations

from sqlalchemy import select

from app.core.security import hash_api_key
from app.models.database.enums import UserRole
from app.models.database.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.strip().lower())
        return (await self.session.scalars(stmt)).one_or_none()

    async def get_by_api_key(self, api_key: str) -> User | None:
        """Look a user up by API key.

        The lookup is by *digest*, so the plaintext key is never compared in SQL and
        never appears in a query log. An inactive user resolves to ``None``.
        """
        stmt = select(User).where(
            User.api_key_hash == hash_api_key(api_key), User.is_active.is_(True)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def create_user(
        self,
        *,
        email: str,
        full_name: str | None = None,
        role: UserRole = UserRole.OPERATOR,
        api_key: str | None = None,
    ) -> User:
        user = User(
            email=email.strip().lower(),
            full_name=full_name,
            role=role,
            api_key_hash=hash_api_key(api_key) if api_key else None,
        )
        return await self.add(user)

    async def get_or_create(
        self, *, email: str, full_name: str | None = None, role: UserRole = UserRole.OPERATOR
    ) -> User:
        """Fetch a user by email, creating them if absent.

        Used only by the demo-user path when authentication is disabled.
        """
        existing = await self.get_by_email(email)
        if existing is not None:
            return existing
        return await self.create_user(email=email, full_name=full_name, role=role)
