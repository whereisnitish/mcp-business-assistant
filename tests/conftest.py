"""Shared test fixtures.

Design decisions worth stating, because they are what make this suite trustworthy:

**No API key, no network, no PostgreSQL.** Every test in the default run is
deterministic and hermetic. The LLM is a scripted fake; the database is SQLite
configured to behave like PostgreSQL (foreign keys on, timezone-aware timestamps).

**Real MCP servers, real protocol.** The ``mcp_manager`` fixture connects to the
actual server objects over the SDK's in-memory transport, so tests exercise the
genuine client, discovery and routing code. Nothing about the protocol is stubbed.
A separate, opt-in suite covers the subprocess transport.

**Isolation by truncation.** Each test starts with empty tables rather than a fresh
database file, which keeps the suite fast while still preventing cross-test bleed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import config as config_module
from app.core.config import Settings
from app.db import session as session_module
from app.mcp.config import MCPServerSpec
from app.mcp.manager import MCPManager
from app.models.database import Base
from app.models.database.enums import UserRole
from app.models.database.user import User
from app.providers.llm.fake_provider import FakeLLMProvider
from app.repositories.user_repository import UserRepository

pytest_plugins: list[str] = []


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def test_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("mba-test")


@pytest.fixture(scope="session")
def settings(test_data_dir: Path) -> Settings:
    """Settings for the whole test session.

    A file-backed SQLite database rather than ``:memory:``: an in-memory database is
    private to a single connection, and both the MCP servers and the API open their
    own, so an in-memory database would leave each of them looking at a different
    (empty) world.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        database_url=f"sqlite+aiosqlite:///{test_data_dir / 'test.db'}",
        auto_create_schema=True,
        seed_demo_data=False,
        auth_enabled=False,
        llm_provider="fake",
        email_provider="mock",
        email_outbox_dir=test_data_dir / "outbox",
        approval_ttl_minutes=30,
        agent_max_iterations=5,
        log_level="WARNING",
        log_format="console",
        mcp_enabled_servers=["crm", "tasks", "spreadsheets", "email", "calendar"],
    )


@pytest.fixture(autouse=True, scope="session")
def _override_global_settings(settings: Settings) -> Iterator[None]:
    """Make ``get_settings()`` return the test settings everywhere.

    Production code legitimately calls ``get_settings()`` deep in the stack (MCP
    tools, repositories). Overriding the cached singleton is what lets those paths
    run against the test database without threading settings through every call.
    """
    config_module.get_settings.cache_clear()
    config_module.get_settings = lambda: settings  # type: ignore[assignment]
    yield
    config_module.get_settings = Settings.__call__  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
async def engine(settings: Settings) -> AsyncIterator[Any]:
    """A fresh engine per test, wired into the application's global accessors.

    Function-scoped on purpose. An async engine holds connections bound to the event
    loop that created them, and pytest-asyncio gives each test its own loop, so a
    session-scoped engine would hand later tests connections belonging to a closed
    loop. Re-creating it is cheap against SQLite and removes a whole class of
    cross-test flakiness.

    Registering it via ``configure_engine`` is what lets code that legitimately
    reaches for the global session factory -- MCP tools, most of all -- run against
    the test database.
    """
    test_engine = create_async_engine(settings.database_url, future=True)
    session_module._configure_sqlite(test_engine)

    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    session_module.configure_engine(test_engine, factory)

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Start every test from empty tables. Reverse dependency order keeps the
        # foreign keys (enforced here, unlike SQLite's default) from blocking it.
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(table.delete())

    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    """A session for direct use in a test."""
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@pytest.fixture
async def user(session: AsyncSession) -> User:
    created = await UserRepository(session).create_user(
        email="operator@example.com", full_name="Operator", role=UserRole.OPERATOR
    )
    await session.commit()
    return created


@pytest.fixture
async def viewer(session: AsyncSession) -> User:
    created = await UserRepository(session).create_user(
        email="viewer@example.com", full_name="Viewer", role=UserRole.VIEWER
    )
    await session.commit()
    return created


@pytest.fixture
async def admin(session: AsyncSession) -> User:
    created = await UserRepository(session).create_user(
        email="admin@example.com", full_name="Admin", role=UserRole.ADMIN
    )
    await session.commit()
    return created


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def in_process_specs() -> list[MCPServerSpec]:
    """Connection specs bound to the real server objects, in this process."""
    from mcp_servers.calendar_server import server as calendar_server
    from mcp_servers.crm_server import server as crm_server
    from mcp_servers.email_server import server as email_server
    from mcp_servers.spreadsheet_server import server as spreadsheet_server
    from mcp_servers.task_server import server as task_server

    return [
        MCPServerSpec(name="crm", transport="in_process", instance=crm_server),
        MCPServerSpec(name="tasks", transport="in_process", instance=task_server),
        MCPServerSpec(name="spreadsheets", transport="in_process", instance=spreadsheet_server),
        MCPServerSpec(name="email", transport="in_process", instance=email_server),
        MCPServerSpec(name="calendar", transport="in_process", instance=calendar_server),
    ]


@pytest.fixture
async def mcp_manager(settings: Settings, engine: Any) -> AsyncIterator[MCPManager]:
    """A connected manager backed by the real MCP servers, in-process."""
    manager = MCPManager(specs=in_process_specs(), settings=settings)
    await manager.startup()
    yield manager
    await manager.shutdown()


@pytest.fixture(autouse=True)
def clean_outbox(settings: Settings) -> Iterator[Path]:
    """Start each test with an empty mock email outbox.

    The outbox is an append-only file in a session-scoped directory, so without this
    a test asserting "nothing was sent" could match text left behind by an earlier
    test -- and would fail (or, worse, pass) for reasons unrelated to its subject.
    """
    outbox = settings.email_outbox_dir / "outbox.jsonl"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.unlink(missing_ok=True)
    yield outbox
    outbox.unlink(missing_ok=True)


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    """An empty scripted provider; tests push responses onto it."""
    return FakeLLMProvider()


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api_client(
    settings: Settings, engine: Any, mcp_manager: MCPManager, fake_llm: FakeLLMProvider
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the ASGI app.

    The lifespan is deliberately **not** run: it would spawn five subprocesses and
    rebuild the database. Instead the process-lifetime objects it would create are
    injected directly, which is exactly what the dependencies read.
    """
    from app.main import create_app

    app = create_app(settings)
    app.state.mcp_manager = mcp_manager
    app.state.llm_provider = fake_llm

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client
