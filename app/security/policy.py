"""The policy engine -- the deterministic authorisation decision.

Every tool call passes through :meth:`PolicyEngine.evaluate` before it can execute.
The decision is a pure function of:

* the tool's permission level, from the local registry,
* the caller's role,
* configuration (``REQUIRE_APPROVAL_FOR_WRITES``),
* the proposed arguments, for a small number of explicit argument-level rules.

It is **not** a function of anything the model wrote. No prompt, no tool output and
no conversation history can influence the outcome. This is what makes the guarantee
survive prompt injection: an attacker who fully controls the model's output can
choose which tool to *propose*, but not whether it runs.

The engine has three possible verdicts:

    ALLOW             execute now
    REQUIRE_APPROVAL  persist the proposal, return to the human, execute only on confirmation
    DENY              refuse outright; no approval can unlock it for this caller

Because the default classification for an unregistered tool is HIGH_RISK, a new or
unexpected tool lands in REQUIRE_APPROVAL rather than ALLOW.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, safe_extra
from app.mcp.types import ToolSpec
from app.models.database.enums import PermissionLevel, UserRole
from app.security import permissions

logger = get_logger(__name__)

#: Sending to more than this many recipients is refused outright rather than merely
#: gated, because it is far more likely to be a runaway loop than an intention.
MAX_EMAIL_RECIPIENTS_HARD_LIMIT = 50

#: Marker inserted by the provider layer when a model emits unparseable arguments.
MALFORMED_ARGUMENTS_KEY = "__malformed_arguments__"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Who is asking, and in what conversation."""

    user_id: str
    user_role: UserRole
    conversation_id: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The verdict for one proposed tool call."""

    outcome: PolicyOutcome
    permission: PermissionLevel
    tool_name: str
    reason: str
    #: Plain-language description of the action, shown to a human approver.
    summary: str
    risk_reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == PolicyOutcome.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.outcome == PolicyOutcome.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.outcome == PolicyOutcome.DENY

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "permission": self.permission.value,
            "tool": self.tool_name,
            "reason": self.reason,
        }


class PolicyEngine:
    """Evaluates proposed tool calls against the permission model."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def evaluate(
        self, spec: ToolSpec, arguments: dict[str, Any], context: PolicyContext
    ) -> PolicyDecision:
        """Decide whether a tool call may proceed.

        Rules are applied in order of severity: a DENY is never downgraded by a
        later rule, which is why the malformed-argument and role checks come first.
        """
        permission = permissions.classify(spec.qualified_name)
        summary = summarise_action(spec, arguments)
        risk = permissions.risk_reason(spec.qualified_name)

        decision = self._first_applicable_rule(spec, arguments, context, permission, summary, risk)

        logger.info(
            "policy decision",
            extra=safe_extra(
                {
                    "event": "policy.decision",
                    "tool": spec.qualified_name,
                    "outcome": decision.outcome.value,
                    "permission": permission.value,
                    "user_role": context.user_role.value,
                }
            ),
        )
        return decision

    def _first_applicable_rule(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        context: PolicyContext,
        permission: PermissionLevel,
        summary: str,
        risk: str,
    ) -> PolicyDecision:
        def decide(outcome: PolicyOutcome, reason: str) -> PolicyDecision:
            return PolicyDecision(
                outcome=outcome,
                permission=permission,
                tool_name=spec.qualified_name,
                reason=reason,
                summary=summary,
                risk_reason=risk,
            )

        # --- 1. Structurally invalid proposals ------------------------------- #
        if MALFORMED_ARGUMENTS_KEY in arguments:
            return decide(
                PolicyOutcome.DENY,
                "The model produced arguments that were not valid JSON.",
            )

        # --- 2. Role restrictions -------------------------------------------- #
        if context.user_role == UserRole.VIEWER and permission != PermissionLevel.READ:
            return decide(
                PolicyOutcome.DENY,
                "Your role only permits read-only tools.",
            )

        # --- 3. Hard argument limits ----------------------------------------- #
        if spec.qualified_name == "email__send_email":
            recipients = _count_recipients(arguments)
            if recipients > MAX_EMAIL_RECIPIENTS_HARD_LIMIT:
                return decide(
                    PolicyOutcome.DENY,
                    f"Refusing to email {recipients} recipients in one action; "
                    f"the hard limit is {MAX_EMAIL_RECIPIENTS_HARD_LIMIT}.",
                )

        # --- 4. Risk-based gating -------------------------------------------- #
        if permission == PermissionLevel.HIGH_RISK:
            return decide(
                PolicyOutcome.REQUIRE_APPROVAL,
                "This action is high risk and needs explicit human approval.",
            )

        if permission == PermissionLevel.WRITE and self._settings.require_approval_for_writes:
            return decide(
                PolicyOutcome.REQUIRE_APPROVAL,
                "Write actions require confirmation in this deployment.",
            )

        if permission == PermissionLevel.WRITE:
            return decide(PolicyOutcome.ALLOW, "Write actions are permitted automatically here.")

        return decide(PolicyOutcome.ALLOW, "Read-only action.")


# --------------------------------------------------------------------------- #
# Human-readable action summaries
# --------------------------------------------------------------------------- #
def summarise_action(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """Describe a proposed action in plain language, for the approver.

    Built by **backend code from the actual arguments**, never taken from model
    output. If the model wrote this text it could describe an email to one person
    while the arguments addressed a hundred, and the human would approve the
    description rather than the action.
    """
    match spec.qualified_name:
        case "email__send_email":
            recipients = _recipient_list(arguments)
            shown = ", ".join(recipients[:5])
            more = f" and {len(recipients) - 5} more" if len(recipients) > 5 else ""
            subject = str(arguments.get("subject", "(no subject)"))
            return (
                f"Send an email with the subject '{subject}' to {len(recipients)} recipient(s): "
                f"{shown}{more}."
            )
        case "calendar__create_event":
            return (
                f"Book '{arguments.get('title', 'an event')}' from {arguments.get('starts_at')} "
                f"to {arguments.get('ends_at')} with "
                f"{len(arguments.get('attendees') or [])} attendee(s)."
            )
        case "crm__create_lead":
            return (
                f"Create a CRM lead for {arguments.get('full_name')} "
                f"({arguments.get('email')}) at {arguments.get('company') or 'no company'}."
            )
        case "crm__update_lead_status":
            return f"Change lead {arguments.get('lead_id')} to status '{arguments.get('status')}'."
        case "tasks__create_task":
            return (
                f"Create the task '{arguments.get('title')}' due "
                f"{arguments.get('due_at') or 'with no deadline'}."
            )
        case "spreadsheets__add_sales_record":
            return (
                f"Record a sale of {arguments.get('quantity')} x {arguments.get('product')} "
                f"to {arguments.get('customer_name')} at {arguments.get('unit_amount')} each."
            )
        case _:
            rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(arguments.items())[:5])
            return f"Run {spec.qualified_name} with {rendered or 'no arguments'}."


def _recipient_list(arguments: dict[str, Any]) -> list[str]:
    recipients = arguments.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    copies = arguments.get("cc") or []
    if isinstance(copies, str):
        copies = [copies]
    return [str(address) for address in [*recipients, *copies]]


def _count_recipients(arguments: dict[str, Any]) -> int:
    return len(_recipient_list(arguments))
