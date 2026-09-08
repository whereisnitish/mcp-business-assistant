"""Email MCP server.

Two tools with deliberately different risk profiles:

* ``draft_email`` composes and validates a message but sends nothing. Safe and
  repeatable, so the agent may call it freely.
* ``send_email`` delivers it. Irreversible, externally visible, and classified
  HIGH_RISK by the policy engine -- the API process will not dispatch a call to it
  without a human approval grant bound to these exact arguments.

Worth being explicit about what protects the send: **not this file**. The server
would happily send if something called it, exactly as an SMTP server would. The
guarantee comes from the API process refusing to dispatch the call until a human
has approved it, which is why the approval check lives there and not here.
"""

from __future__ import annotations

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.integrations.factory import build_email
from app.models.schemas.domain import EmailDraftDTO, EmailSendResultDTO
from mcp_servers.common.runtime import build_server, run_tool

logger = get_logger(__name__)

INSTRUCTIONS = """\
Email tools.

draft_email prepares a message and returns it for review WITHOUT sending anything;
use it to show the user what you intend to send. send_email actually delivers the
message and requires human approval before it will run. Always draft before
sending, and never invent recipient addresses -- take them from CRM results.
"""

server = build_server("email", instructions=INSTRUCTIONS)


@server.tool(
    description=(
        "Compose and validate an email WITHOUT sending it. Returns the prepared draft "
        "for review. Always use this before send_email."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def draft_email(
    to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
    subject: Annotated[str, Field(min_length=1, max_length=300, description="Subject line.")],
    body: Annotated[str, Field(min_length=1, max_length=20000, description="Plain-text body.")],
    cc: Annotated[list[str] | None, Field(description="Addresses to copy.")] = None,
    reply_to: Annotated[str | None, Field(description="Reply-To address.")] = None,
) -> EmailDraftDTO:
    async def _run() -> EmailDraftDTO:
        return await build_email().draft_email(
            to=to, subject=subject, body=body, cc=cc, reply_to=reply_to
        )

    return await run_tool("draft_email", _run)


@server.tool(
    description=(
        "Send an email. This is irreversible and visible to the recipient, so it "
        "requires explicit human approval before it will execute."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False),
)
async def send_email(
    to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
    subject: Annotated[str, Field(min_length=1, max_length=300, description="Subject line.")],
    body: Annotated[str, Field(min_length=1, max_length=20000, description="Plain-text body.")],
    cc: Annotated[list[str] | None, Field(description="Addresses to copy.")] = None,
    reply_to: Annotated[str | None, Field(description="Reply-To address.")] = None,
) -> EmailSendResultDTO:
    async def _run() -> EmailSendResultDTO:
        provider = build_email()
        result = await provider.send_email(
            to=to, subject=subject, body=body, cc=cc, reply_to=reply_to
        )
        logger.info(
            "send_email tool completed",
            extra={
                "event": "mcp.email_sent",
                "provider": provider.provider_name,
                "is_mock": provider.is_mock,
                "recipient_count": len(result.to),
            },
        )
        return result

    return await run_tool("send_email", _run)
