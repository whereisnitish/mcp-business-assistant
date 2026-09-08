"""Application configuration.

All configuration is environment driven (12-factor). Secrets are held in
``SecretStr`` so that an accidental ``repr()``/log of the settings object cannot
leak them -- see :mod:`app.core.security` for the complementary redaction helpers.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
LLMProviderName = Literal["openai", "openrouter", "openai_compatible", "heuristic", "fake"]
MCPTransport = Literal["stdio", "http"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed application settings loaded from the environment and/or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app --
    app_name: str = "MCP Business Assistant"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ----------------------------------------------------------------- auth --
    # Disabled by default so the project is trivially runnable, but the API layer
    # emits a loud warning whenever it is disabled outside `local`/`test`.
    auth_enabled: bool = False
    api_key: SecretStr | None = None
    demo_user_email: str = "demo@example.com"

    # ------------------------------------------------------------- database --
    database_url: str = "postgresql+asyncpg://mcp:mcp@localhost:5432/mcp_assistant"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20
    auto_create_schema: bool = True
    seed_demo_data: bool = False
    """Insert illustrative leads/tasks/sales on startup when the tables are empty."""

    # ------------------------------------------------------------------ llm --
    llm_provider: LLMProviderName = "heuristic"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # ---------------------------------------------------------------- agent --
    agent_max_iterations: int = 6
    agent_tool_timeout_seconds: float = 30.0
    agent_history_limit: int = 20

    # ------------------------------------------------------------------ mcp --
    mcp_transport: MCPTransport = "stdio"
    mcp_enabled_servers: list[str] = Field(
        default_factory=lambda: ["crm", "tasks", "spreadsheets", "email", "calendar"]
    )
    mcp_startup_timeout_seconds: float = 45.0
    """Per-server connect budget. Cold-starting a Python subprocess and importing
    SQLAlchemy/pydantic costs several seconds, and more on Windows, so this is
    generous by default; servers connect concurrently, not one after another."""
    mcp_call_timeout_seconds: float = 30.0
    mcp_http_base_url: str = "http://localhost:9000"
    mcp_fail_fast: bool = False
    """When false the API starts even if some MCP servers are unreachable (degraded mode)."""

    # ------------------------------------------------------------- approval --
    approval_ttl_minutes: int = 30
    require_approval_for_writes: bool = False
    """HIGH_RISK always requires approval; this promotes WRITE tools to the same bar."""

    # -------------------------------------------------------- integrations --
    crm_provider: Literal["local", "hubspot"] = "local"
    hubspot_access_token: SecretStr | None = None

    email_provider: Literal["mock", "smtp"] = "mock"
    email_from_address: str = "assistant@example.com"
    email_outbox_dir: Path = PROJECT_ROOT / "data" / "outbox"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True

    spreadsheet_provider: Literal["local", "google_sheets"] = "local"
    google_sheets_spreadsheet_id: str | None = None
    google_service_account_file: Path | None = None

    # ------------------------------------------------------------ telemetry --
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --------------------------------------------------------------- helpers --
    @field_validator("cors_origins", "mcp_enabled_servers", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow ``A,B,C`` in the environment as well as a JSON list."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_provider_credentials(self) -> Settings:
        if (
            self.llm_provider in ("openai", "openrouter", "openai_compatible")
            and not self.llm_api_key
        ):
            raise ValueError(
                f"llm_provider='{self.llm_provider}' requires LLM_API_KEY to be set. "
                "Use LLM_PROVIDER=heuristic for a credential-free local demo."
            )
        if self.email_provider == "smtp" and not self.smtp_host:
            raise ValueError("email_provider='smtp' requires SMTP_HOST to be set.")
        if self.crm_provider == "hubspot" and not self.hubspot_access_token:
            raise ValueError("crm_provider='hubspot' requires HUBSPOT_ACCESS_TOKEN to be set.")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that every dependency-injected consumer sees the same object; tests
    clear the cache via the ``settings`` fixture.
    """
    return Settings()
