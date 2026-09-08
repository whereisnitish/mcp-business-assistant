"""The security core: permission classification and policy decisions.

These are the highest-value tests in the suite. If the policy engine is wrong, an
autonomous agent performs irreversible actions without asking -- so the properties
asserted here are the ones that must never silently regress.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.database.enums import PermissionLevel, UserRole
from app.security import permissions
from app.security.policy import (
    MAX_EMAIL_RECIPIENTS_HARD_LIMIT,
    PolicyEngine,
    PolicyOutcome,
    summarise_action,
)
from tests.fixtures.factories import make_policy_context, make_tool_spec

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
class TestPermissionRegistry:
    def test_unregistered_tool_is_high_risk(self) -> None:
        """The single most important property: unknown tools fail closed.

        Connecting a new MCP server must never silently grant its tools automatic
        execution.
        """
        assert permissions.classify("evil__wipe_database") == PermissionLevel.HIGH_RISK
        assert permissions.classify("") == PermissionLevel.HIGH_RISK
        assert not permissions.is_registered("evil__wipe_database")

    @pytest.mark.parametrize(
        ("tool", "expected"),
        [
            ("crm__get_leads", PermissionLevel.READ),
            ("crm__search_leads", PermissionLevel.READ),
            ("tasks__get_overdue_tasks", PermissionLevel.READ),
            ("spreadsheets__get_sales_summary", PermissionLevel.READ),
            ("email__draft_email", PermissionLevel.READ),
            ("crm__create_lead", PermissionLevel.WRITE),
            ("tasks__create_task", PermissionLevel.WRITE),
            ("spreadsheets__add_sales_record", PermissionLevel.WRITE),
            ("email__send_email", PermissionLevel.HIGH_RISK),
            ("calendar__create_event", PermissionLevel.HIGH_RISK),
        ],
    )
    def test_known_tools_are_classified_as_documented(
        self, tool: str, expected: PermissionLevel
    ) -> None:
        assert permissions.classify(tool) == expected

    def test_sending_email_is_never_merely_a_write(self) -> None:
        """Irreversible, externally visible actions must sit above WRITE."""
        assert permissions.classify("email__send_email") == PermissionLevel.HIGH_RISK

    def test_drafting_is_separated_from_sending(self) -> None:
        """Drafting must stay cheap, or the agent cannot show work before acting."""
        assert permissions.classify("email__draft_email") == PermissionLevel.READ

    def test_every_registered_tool_has_a_risk_reason(self) -> None:
        for tool in permissions.TOOL_PERMISSIONS:
            assert permissions.risk_reason(tool).strip()

    def test_risk_reason_flags_an_unclassified_tool(self) -> None:
        assert "not in the permission registry" in permissions.risk_reason("mystery__tool")


# --------------------------------------------------------------------------- #
# Policy decisions
# --------------------------------------------------------------------------- #
@pytest.fixture
def policy(settings: Settings) -> PolicyEngine:
    """Default policy: writes run automatically, high risk needs approval."""
    return PolicyEngine(settings)


@pytest.fixture
def strict_policy(settings: Settings) -> PolicyEngine:
    """A deployment that also holds WRITE tools behind confirmation."""
    return PolicyEngine(settings.model_copy(update={"require_approval_for_writes": True}))


class TestPolicyEngine:
    def test_read_tools_run_automatically(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("crm__get_leads", permission=PermissionLevel.READ),
            {},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.allowed

    def test_write_tools_run_automatically_by_default(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
            {"full_name": "Ada", "email": "ada@example.com"},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_writes_can_be_promoted_to_requiring_approval(
        self, strict_policy: PolicyEngine
    ) -> None:
        decision = strict_policy.evaluate(
            make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
            {},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    def test_high_risk_always_requires_approval(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": ["a@b.com"], "subject": "Hi"},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert decision.needs_approval

    def test_high_risk_requires_approval_even_for_an_admin(self, policy: PolicyEngine) -> None:
        """Seniority is not a substitute for confirmation.

        An admin may *decide* approvals; that does not mean their agent skips them.
        """
        decision = policy.evaluate(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": ["a@b.com"]},
            make_policy_context(role=UserRole.ADMIN),
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    def test_viewers_cannot_write(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
            {},
            make_policy_context(role=UserRole.VIEWER),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "read-only" in decision.reason

    def test_viewers_may_still_read(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("crm__get_leads", permission=PermissionLevel.READ),
            {},
            make_policy_context(role=UserRole.VIEWER),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_a_viewer_denial_is_not_downgraded_to_an_approval(self, policy: PolicyEngine) -> None:
        """A denial must not become "just ask someone" for a high-risk tool."""
        decision = policy.evaluate(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": ["a@b.com"]},
            make_policy_context(role=UserRole.VIEWER),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_unknown_tools_require_approval(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(
            make_tool_spec("unknown__tool", permission=PermissionLevel.HIGH_RISK),
            {},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    def test_malformed_arguments_are_denied(self, policy: PolicyEngine) -> None:
        """A model emitting unparseable JSON must not reach a tool."""
        decision = policy.evaluate(
            make_tool_spec("crm__get_leads", permission=PermissionLevel.READ),
            {"__malformed_arguments__": "{not json"},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_mass_email_is_denied_outright(self, policy: PolicyEngine) -> None:
        """Beyond the hard limit this is a runaway loop, not a decision to delegate."""
        recipients = [
            f"user{index}@example.com" for index in range(MAX_EMAIL_RECIPIENTS_HARD_LIMIT + 1)
        ]
        decision = policy.evaluate(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": recipients},
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_recipient_limit_counts_cc(self, policy: PolicyEngine) -> None:
        """Splitting recipients across to/cc must not evade the limit."""
        half = MAX_EMAIL_RECIPIENTS_HARD_LIMIT // 2 + 1
        decision = policy.evaluate(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {
                "to": [f"a{index}@example.com" for index in range(half)],
                "cc": [f"b{index}@example.com" for index in range(half)],
            },
            make_policy_context(),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_server_read_only_hint_does_not_override_policy(self, policy: PolicyEngine) -> None:
        """A hostile or misconfigured MCP server cannot declare itself safe.

        The server claims read-only; the local registry says high risk. The local
        registry must win.
        """
        spec = make_tool_spec(
            "email__send_email", permission=PermissionLevel.HIGH_RISK, server_read_only_hint=True
        )
        assert spec.hint_conflicts_with_policy
        decision = policy.evaluate(spec, {"to": ["a@b.com"]}, make_policy_context())
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    def test_classification_ignores_the_spec_object(self, policy: PolicyEngine) -> None:
        """Permission comes from the registry, not from the passed-in ToolSpec.

        Guards against a compromised discovery path handing over a spec that
        mislabels a dangerous tool as READ.
        """
        lying_spec = make_tool_spec("email__send_email", permission=PermissionLevel.READ)
        decision = policy.evaluate(lying_spec, {"to": ["a@b.com"]}, make_policy_context())
        assert decision.permission is PermissionLevel.HIGH_RISK
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


# --------------------------------------------------------------------------- #
# Approval summaries
# --------------------------------------------------------------------------- #
class TestActionSummaries:
    def test_email_summary_states_recipient_count_and_subject(self) -> None:
        summary = summarise_action(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": ["a@example.com", "b@example.com"], "subject": "Q3 offer", "body": "..."},
        )
        assert "2 recipient(s)" in summary
        assert "Q3 offer" in summary
        assert "a@example.com" in summary

    def test_large_recipient_lists_are_truncated_but_counted(self) -> None:
        """An approver must see the true scale even when the list is abbreviated."""
        recipients = [f"user{index}@example.com" for index in range(12)]
        summary = summarise_action(
            make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            {"to": recipients, "subject": "Hi"},
        )
        assert "12 recipient(s)" in summary
        assert "and 7 more" in summary

    def test_summary_is_generated_for_unknown_tools_too(self) -> None:
        summary = summarise_action(make_tool_spec("mystery__thing"), {"alpha": 1})
        assert "mystery__thing" in summary
