"""MCP server registry: how to reach each server.

The set of servers is configuration, not code. Adding a sixth server means adding
an entry here (or pointing ``MCP_ENABLED_SERVERS`` at an existing deployment) --
the agent, the API and the prompts are untouched, because tools are discovered at
runtime rather than declared in the agent.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import StdioServerParameters

from app.core.config import PROJECT_ROOT, Settings, get_settings

#: Module path of each server's ``__main__``, launched as ``python -m <module>``.
SERVER_MODULES: dict[str, str] = {
    "crm": "mcp_servers.crm_server",
    "tasks": "mcp_servers.task_server",
    "spreadsheets": "mcp_servers.spreadsheet_server",
    "email": "mcp_servers.email_server",
    "calendar": "mcp_servers.calendar_server",
}

#: Default port per server when running them as standalone HTTP services.
SERVER_HTTP_PORTS: dict[str, int] = {
    "crm": 9001,
    "tasks": 9002,
    "spreadsheets": 9003,
    "email": 9004,
    "calendar": 9005,
}

# --------------------------------------------------------------------------- #
# Child environment
#
# The MCP SDK does NOT hand a stdio child the parent's environment. It inherits a
# short allow-list (PATH, HOME and similar) and nothing else, so `DATABASE_URL`
# would not reach the server and it would silently fall back to the default
# connection string. Every variable a server needs must therefore be passed
# explicitly.
#
# That constraint is worth leaning into rather than working around: the lists below
# are per-server, so each subprocess receives only the credentials its own tools
# require. The email server never sees the HubSpot token; the CRM server never sees
# the SMTP password. If one tool server is compromised, the blast radius stops at
# the secrets that server was given.
# --------------------------------------------------------------------------- #

#: Passed to every server: they all open the database and emit logs.
SHARED_ENV_VARS: tuple[str, ...] = (
    "DATABASE_URL",
    "DATABASE_ECHO",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "ENVIRONMENT",
)

#: Additional variables per server -- deliberately minimal.
SERVER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "crm": ("CRM_PROVIDER", "HUBSPOT_ACCESS_TOKEN"),
    "tasks": (),
    "calendar": (),
    "spreadsheets": (
        "SPREADSHEET_PROVIDER",
        "GOOGLE_SHEETS_SPREADSHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
    ),
    "email": (
        "EMAIL_PROVIDER",
        "EMAIL_FROM_ADDRESS",
        "EMAIL_OUTBOX_DIR",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_USE_TLS",
    ),
}


def build_child_env(server_name: str) -> dict[str, str]:
    """Environment for one MCP server subprocess.

    Only variables that are actually set are forwarded, so an unset optional
    credential stays unset in the child rather than becoming an empty string --
    which pydantic-settings would treat as a supplied (and invalid) value.
    """
    wanted = SHARED_ENV_VARS + SERVER_ENV_VARS.get(server_name, ())
    env = {name: os.environ[name] for name in wanted if os.environ.get(name)}

    # Ensure `python -m mcp_servers.<name>` resolves regardless of the working
    # directory the API was started from.
    existing_path = os.environ.get("PYTHONPATH", "")
    root = str(PROJECT_ROOT)
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing_path}" if existing_path else root
    return env


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """How to connect to one MCP server.

    ``in_process`` is a third transport used by the test suite: the SDK's ``Client``
    accepts a server *instance* and speaks the real protocol over in-memory streams.
    Tests therefore exercise the genuine client, connection and manager code against
    genuine servers, without spawning subprocesses -- so a protocol-level regression
    still fails the fast test suite rather than only the slow one.
    """

    name: str
    transport: Literal["stdio", "http", "in_process"]
    module: str | None = None
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    instance: Any = None
    """An ``MCPServer`` object, required when ``transport == "in_process"``."""

    def stdio_parameters(self) -> StdioServerParameters:
        """Parameters for launching this server as a subprocess.

        ``sys.executable`` is used rather than a bare ``"python"`` so the child runs
        in the same interpreter and virtualenv as the API -- otherwise the server
        would resolve a different (or missing) dependency set.

        Configuration reaches the child through :func:`build_child_env`, never
        through command-line arguments, where a credential would be visible to
        anyone who can list processes.
        """
        if self.module is None:
            raise ValueError(f"MCP server {self.name!r} has no module to launch.")
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self.module],
            env=self.env or build_child_env(self.name),
        )

    def target(self) -> Any:
        """The connection target accepted by :class:`mcp.Client`."""
        if self.transport == "in_process":
            if self.instance is None:
                raise ValueError(f"MCP server {self.name!r} has no server instance to connect to.")
            return self.instance
        if self.transport == "http":
            if not self.url:
                raise ValueError(f"MCP server {self.name!r} has no URL configured.")
            return self.url
        return self.stdio_parameters()


def build_server_specs(settings: Settings | None = None) -> list[MCPServerSpec]:
    """Build the connection specs for every enabled server.

    Unknown names in ``MCP_ENABLED_SERVERS`` are skipped rather than raising, so a
    typo degrades one capability instead of preventing the API from starting; the
    manager logs the omission at startup.
    """
    settings = settings or get_settings()
    specs: list[MCPServerSpec] = []

    for name in settings.mcp_enabled_servers:
        module = SERVER_MODULES.get(name)
        if module is None:
            continue
        if settings.mcp_transport == "http":
            base = settings.mcp_http_base_url.rstrip("/")
            specs.append(
                MCPServerSpec(name=name, transport="http", url=f"{base}/{name}/mcp", module=module)
            )
        else:
            specs.append(MCPServerSpec(name=name, transport="stdio", module=module))
    return specs


def unknown_servers(settings: Settings | None = None) -> list[str]:
    """Configured server names with no known implementation."""
    settings = settings or get_settings()
    return [name for name in settings.mcp_enabled_servers if name not in SERVER_MODULES]
