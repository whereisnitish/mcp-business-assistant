"""Email interface.

Drafting and sending are deliberately separate operations. Drafting is safe and
reversible, so the agent may do it freely (READ level). Sending is irreversible and
externally visible, so it is HIGH_RISK and cannot execute without a human approval
grant. Splitting them is what makes "prepare the email, then ask" expressible.
"""

from __future__ import annotations

from abc import abstractmethod

from app.integrations.base import BusinessIntegration
from app.models.schemas.domain import EmailDraftDTO, EmailSendResultDTO

#: Upper bound on recipients for a single send. A model that decides to email
#: "all leads" cannot turn one approval into an unbounded blast.
MAX_RECIPIENTS = 50


class EmailInterface(BusinessIntegration):
    """Compose and deliver email."""

    @abstractmethod
    async def draft_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: str | None = None,
    ) -> EmailDraftDTO:
        """Validate and prepare an email without sending it."""

    @abstractmethod
    async def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: str | None = None,
    ) -> EmailSendResultDTO:
        """Deliver an email.

        Implementations must assume this is reachable only after human approval;
        they are not the place where the approval decision is made.
        """
