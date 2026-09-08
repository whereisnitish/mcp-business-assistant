"""SMTP email provider -- real delivery.

Uses the standard library's ``smtplib``, which is synchronous, so the blocking
send runs in a worker thread via :func:`asyncio.to_thread`. Blocking the event loop
inside an async service would stall every other in-flight request, and an SMTP
handshake against a slow server can take seconds.

Not covered by the default test suite (it would require a live mail server); the
optional ``@pytest.mark.live`` suite exercises it when credentials are configured.
"""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

from app.core.config import Settings
from app.core.logging import get_logger, safe_extra
from app.integrations.base import IntegrationError, IntegrationNotConfiguredError
from app.integrations.email.base import EmailInterface
from app.integrations.email.mock import build_draft
from app.models.database.enums import EmailStatus
from app.models.schemas.domain import EmailDraftDTO, EmailSendResultDTO

logger = get_logger(__name__)


class SMTPEmailProvider(EmailInterface):
    """Delivers email over SMTP."""

    provider_name = "smtp"

    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise IntegrationNotConfiguredError("smtp", "SMTP_HOST is not set")
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = settings.smtp_password
        self._use_tls = settings.smtp_use_tls
        self._from_address = settings.email_from_address
        self._timeout = 30.0

    async def health_check(self) -> bool:
        """Open a connection and disconnect without sending anything."""
        try:
            await asyncio.to_thread(self._probe)
        except (OSError, smtplib.SMTPException):
            logger.warning(
                "SMTP health check failed", extra=safe_extra({"event": "email.smtp_unhealthy"})
            )
            return False
        return True

    def _probe(self) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
            server.noop()

    async def draft_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: str | None = None,
    ) -> EmailDraftDTO:
        return build_draft(to=to, subject=subject, body=body, cc=cc, reply_to=reply_to)

    async def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: str | None = None,
    ) -> EmailSendResultDTO:
        draft = build_draft(to=to, subject=subject, body=body, cc=cc, reply_to=reply_to)
        message_id = f"<{uuid.uuid4().hex}@{self._host}>"

        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = ", ".join(str(address) for address in draft.to)
        if draft.cc:
            message["Cc"] = ", ".join(str(address) for address in draft.cc)
        if draft.reply_to:
            message["Reply-To"] = draft.reply_to
        message["Subject"] = draft.subject
        message["Message-ID"] = message_id
        message.set_content(draft.body)

        try:
            await asyncio.to_thread(self._deliver, message)
        except smtplib.SMTPAuthenticationError as exc:
            raise IntegrationError("smtp", "authentication rejected by the mail server") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            # Report how many were refused, never which addresses -- the audit log
            # already holds the approved recipient list.
            raise IntegrationError(
                "smtp", f"{len(exc.recipients)} recipient(s) were refused by the mail server"
            ) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise IntegrationError("smtp", f"delivery failed: {type(exc).__name__}") from exc

        sent_at = datetime.now(UTC)
        logger.info(
            "email sent via SMTP",
            extra=safe_extra(
                {
                    "event": "email.sent",
                    "message_id": message_id,
                    "recipient_count": draft.recipient_count,
                }
            ),
        )
        return EmailSendResultDTO(
            status=EmailStatus.SENT,
            message_id=message_id,
            to=[str(address) for address in draft.to],
            subject=draft.subject,
            provider=self.provider_name,
            sent_at=sent_at,
        )

    def _deliver(self, message: EmailMessage) -> None:
        """Blocking SMTP delivery. Always called through ``asyncio.to_thread``."""
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
            if self._use_tls:
                server.starttls()
            if self._username and self._password:
                server.login(self._username, self._password.get_secret_value())
            server.send_message(message)
