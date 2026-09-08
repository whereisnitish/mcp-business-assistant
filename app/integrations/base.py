"""Integration layer foundations.

Every business capability is expressed as an abstract interface. MCP tools depend
on the *interface*, never on a concrete provider, which is the seam that lets a
local PostgreSQL implementation be replaced by HubSpot, Gmail or Google Sheets
without changing a single tool signature -- and therefore without the agent, its
prompts or any client noticing.

    CRMInterface
    |-- LocalCRMRepository        (PostgreSQL, the default)
    +-- HubSpotCRMIntegration     (real HubSpot REST API)

    EmailInterface
    |-- MockEmailProvider         (DEV MOCK -- writes to a JSONL outbox)
    +-- SMTPEmailProvider         (real SMTP delivery)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.core.exceptions import AppError


class IntegrationError(AppError):
    """A downstream business system failed.

    Carries the provider name so the audit trail records *which* system failed,
    while the message stays client-safe.
    """

    code = "integration_error"
    status_code = 502
    message = "A downstream business system failed."

    def __init__(
        self, provider: str, reason: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            f"Integration {provider!r} failed: {reason}",
            details={"provider": provider, **(details or {})},
        )
        self.provider = provider


class IntegrationNotConfiguredError(IntegrationError):
    """The provider was selected but its credentials or settings are missing."""

    code = "integration_not_configured"
    status_code = 500


class BusinessIntegration(ABC):
    """Common surface shared by every integration implementation."""

    #: Stable provider identifier recorded in audit logs (``"local"``, ``"hubspot"``...).
    provider_name: str = "unknown"

    @property
    def is_mock(self) -> bool:
        """True for development stand-ins that do not touch a real external system.

        Surfaced through ``/health`` and the audit trail so a mocked send is never
        mistaken for a real one.
        """
        return False

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True when the integration is usable right now."""
