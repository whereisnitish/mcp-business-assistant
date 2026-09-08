"""DEVELOPMENT MOCK email provider.

Writes every "sent" message to a JSONL outbox file instead of delivering it. This
is the default provider so that the demo -- including the full approval workflow --
can be exercised end to end without a mail server and without the risk of a test
run emailing a real person.

It is explicitly labelled: ``is_mock`` is ``True``, ``provider_name`` is ``"mock"``,
and every send is logged and returned with ``provider="mock"``, so a mocked send can
never be mistaken for a real one in the audit trail.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from app.core.logging import get_logger, safe_extra
from app.integrations.base import IntegrationError
from app.integrations.email.base import MAX_RECIPIENTS, EmailInterface
from app.models.database.enums import EmailStatus
from app.models.schemas.domain import EmailDraftDTO, EmailSendResultDTO

logger = get_logger(__name__)


class MockEmailProvider(EmailInterface):
    """Records email to a local JSONL outbox. Sends nothing."""

    provider_name = "mock"

    def __init__(self, outbox_dir: Path, from_address: str = "assistant@example.com") -> None:
        self._outbox_dir = Path(outbox_dir)
        self._from_address = from_address

    @property
    def is_mock(self) -> bool:
        return True

    @property
    def outbox_path(self) -> Path:
        return self._outbox_dir / "outbox.jsonl"

    async def health_check(self) -> bool:
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        return True

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
        message_id = f"mock-{uuid.uuid4().hex}"
        sent_at = datetime.now(UTC)

        record = {
            "message_id": message_id,
            "from": self._from_address,
            "to": [str(address) for address in draft.to],
            "cc": [str(address) for address in draft.cc],
            "reply_to": draft.reply_to,
            "subject": draft.subject,
            "body": draft.body,
            "sent_at": sent_at.isoformat(),
            "provider": self.provider_name,
        }
        try:
            self._outbox_dir.mkdir(parents=True, exist_ok=True)
            with self.outbox_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise IntegrationError(
                "mock_email", f"could not write to the outbox: {exc.strerror}"
            ) from exc

        logger.warning(
            "MOCK email recorded -- nothing was actually delivered",
            extra=safe_extra(
                {
                    "event": "email.mock_sent",
                    "message_id": message_id,
                    "recipient_count": draft.recipient_count,
                    "subject": draft.subject,
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
            detail=(
                f"DEVELOPMENT MOCK: recorded to {self.outbox_path.name}; no email was delivered."
            ),
        )


def build_draft(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_to: str | None = None,
) -> EmailDraftDTO:
    """Validate and normalise a draft. Shared by every email provider.

    Enforces recipient limits and address syntax here, at the integration boundary,
    so no provider can skip the check. These are validity rules, not authorisation
    -- the approval decision belongs to the policy engine.
    """
    recipients = [address.strip() for address in to if address and address.strip()]
    copies = [address.strip() for address in (cc or []) if address and address.strip()]

    if not recipients:
        raise IntegrationError("email", "at least one recipient is required")
    total = len(recipients) + len(copies)
    if total > MAX_RECIPIENTS:
        raise IntegrationError(
            "email",
            f"too many recipients ({total}); the limit is {MAX_RECIPIENTS}",
            details={"recipient_count": total, "limit": MAX_RECIPIENTS},
        )
    if not subject.strip():
        raise IntegrationError("email", "a subject is required")
    if not body.strip():
        raise IntegrationError("email", "a body is required")

    try:
        return EmailDraftDTO(
            to=recipients,
            cc=copies,
            subject=subject.strip(),
            body=body,
            reply_to=reply_to,
            recipient_count=total,
            preview=body.strip()[:200],
        )
    except PydanticValidationError as exc:
        invalid = sorted({str(error["input"]) for error in exc.errors() if "input" in error})
        raise IntegrationError(
            "email", f"invalid email address: {', '.join(invalid) or 'unknown'}"
        ) from exc
