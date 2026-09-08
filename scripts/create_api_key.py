"""Create a user and issue an API key.

Needed once ``AUTH_ENABLED=true``, since only the *digest* of a key is stored and the
plaintext is therefore unrecoverable afterwards. It is printed exactly once, here.

Usage:
    python scripts/create_api_key.py --email alice@example.com --role operator
    python scripts/create_api_key.py --email admin@example.com --role admin --name "Ops Admin"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.core.security import generate_api_key
from app.db.session import create_all, session_scope
from app.models.database.enums import UserRole
from app.repositories.user_repository import UserRepository


async def create(email: str, full_name: str | None, role: UserRole) -> int:
    settings = get_settings()
    await create_all(settings)

    async with session_scope(settings) as session:
        users = UserRepository(session)
        if await users.get_by_email(email) is not None:
            print(f"A user with the email {email!r} already exists.", file=sys.stderr)
            return 1

        api_key = generate_api_key()
        user = await users.create_user(email=email, full_name=full_name, role=role, api_key=api_key)

    print("User created.")
    print(f"  id:    {user.id}")
    print(f"  email: {user.email}")
    print(f"  role:  {user.role.value}")
    print()
    print("API key (shown once -- only its SHA-256 digest is stored):")
    print(f"  {api_key}")
    print()
    print("Use it as a header:")
    print(f'  curl -H "X-API-Key: {api_key}" http://localhost:8000/api/v1/tools')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Email address; must be unique.")
    parser.add_argument("--name", default=None, help="Display name.")
    parser.add_argument(
        "--role",
        default=UserRole.OPERATOR.value,
        choices=[role.value for role in UserRole],
        help="viewer (read-only tools), operator (default), or admin (may decide any approval).",
    )
    args = parser.parse_args()
    return asyncio.run(create(args.email, args.name, UserRole(args.role)))


if __name__ == "__main__":
    sys.exit(main())
