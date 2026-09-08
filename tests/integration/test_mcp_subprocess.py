"""Real stdio transport: MCP servers as actual subprocesses.

Marked ``slow`` and excluded from the default run, because launching five Python
interpreters costs tens of seconds. It is not optional in spirit -- run it in CI --
but it should not sit between a developer and fast feedback.

Run it with::

    pytest -m slow

What it covers that the in-process suite cannot:

* the stdio transport itself (framing, process lifecycle, clean shutdown);
* **environment propagation**, which is easy to get wrong and silent when it is.
  The SDK gives a stdio child only a small allow-list of inherited variables, so
  ``DATABASE_URL`` must be passed explicitly. If that regressed, the servers would
  quietly connect to a different database and every in-process test would still pass.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.mcp.config import MCPServerSpec, build_child_env
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall
from app.repositories.lead_repository import LeadRepository

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
async def subprocess_manager(settings: Settings, engine: object, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A manager driving two servers as real subprocesses.

    ``DATABASE_URL`` is placed in the environment because that is exactly the path
    under test: the child inherits configuration through the explicit allow-list in
    :func:`app.mcp.config.build_child_env`, not through the parent's whole environment.
    """
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("EMAIL_OUTBOX_DIR", str(settings.email_outbox_dir))

    manager = MCPManager(
        specs=[
            MCPServerSpec(name="crm", transport="stdio", module="mcp_servers.crm_server"),
            MCPServerSpec(name="tasks", transport="stdio", module="mcp_servers.task_server"),
        ],
        settings=settings,
    )
    await manager.startup()
    yield manager
    await manager.shutdown()


class TestChildEnvironment:
    def test_database_url_is_forwarded_explicitly(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", settings.database_url)
        env = build_child_env("crm")

        assert env["DATABASE_URL"] == settings.database_url
        assert "PYTHONPATH" in env

    def test_secrets_are_scoped_to_the_server_that_needs_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Least privilege: one compromised tool server must not expose every credential."""
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-secret")
        monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")

        crm_env = build_child_env("crm")
        email_env = build_child_env("email")
        tasks_env = build_child_env("tasks")

        assert crm_env["HUBSPOT_ACCESS_TOKEN"] == "pat-secret"
        assert "SMTP_PASSWORD" not in crm_env
        assert email_env["SMTP_PASSWORD"] == "smtp-secret"
        assert "HUBSPOT_ACCESS_TOKEN" not in email_env
        assert "SMTP_PASSWORD" not in tasks_env
        assert "HUBSPOT_ACCESS_TOKEN" not in tasks_env

    def test_unset_variables_are_not_forwarded_as_empty_strings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string would look like a supplied (invalid) value to settings."""
        monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)
        assert "HUBSPOT_ACCESS_TOKEN" not in build_child_env("crm")


class TestStdioTransport:
    async def test_servers_start_and_publish_their_tools(
        self, subprocess_manager: MCPManager
    ) -> None:
        catalog = subprocess_manager.catalog

        assert not subprocess_manager.is_degraded
        assert "crm__get_leads" in catalog
        assert "tasks__create_task" in catalog

    async def test_a_write_in_a_child_process_is_visible_to_the_parent(
        self, subprocess_manager: MCPManager, session: AsyncSession
    ) -> None:
        """Proves the child really is using the configured database."""
        result = await subprocess_manager.call_tool(
            ToolCall(
                id="1",
                tool_name="crm__create_lead",
                arguments={"full_name": "Subprocess Lead", "email": "sub@example.com"},
            )
        )
        assert result.success

        lead = await LeadRepository(session).get_by_email("sub@example.com")
        assert lead is not None and lead.full_name == "Subprocess Lead"

    async def test_health_reports_reachable_servers(self, subprocess_manager: MCPManager) -> None:
        health = await subprocess_manager.health()
        assert health["degraded"] is False
        assert all(server["reachable"] for server in health["servers"].values())


class TestDegradedMode:
    async def test_an_unreachable_server_does_not_prevent_startup(
        self, settings: Settings, engine: object
    ) -> None:
        """A missing email server must not take the whole assistant offline."""
        manager = MCPManager(
            specs=[
                MCPServerSpec(name="crm", transport="stdio", module="mcp_servers.crm_server"),
                MCPServerSpec(name="tasks", transport="stdio", module="mcp_servers.does_not_exist"),
            ],
            settings=settings.model_copy(update={"mcp_startup_timeout_seconds": 20.0}),
        )
        await manager.startup()
        try:
            assert manager.is_degraded
            assert "tasks" in manager.failures
            assert "crm__get_leads" in manager.catalog
        finally:
            await manager.shutdown()

    async def test_fail_fast_refuses_to_start_when_configured(
        self, settings: Settings, engine: object
    ) -> None:
        from app.core.exceptions import MCPServerUnavailableError

        manager = MCPManager(
            specs=[
                MCPServerSpec(name="tasks", transport="stdio", module="mcp_servers.does_not_exist")
            ],
            settings=settings.model_copy(
                update={"mcp_fail_fast": True, "mcp_startup_timeout_seconds": 20.0}
            ),
        )
        with pytest.raises(MCPServerUnavailableError):
            await manager.startup()
