"""MCP client-layer units: naming, result mapping, discovery and argument validation."""

from __future__ import annotations

import pytest

from app.mcp.discovery import ToolCatalog, build_tool_spec
from app.mcp.types import NAME_SEPARATOR, ToolCall, ToolResult, qualify, split_qualified
from app.mcp.validation import validate_arguments
from app.models.database.enums import PermissionLevel
from tests.fixtures.factories import make_tool_spec

pytestmark = pytest.mark.unit


class FakeTool:
    """Stands in for an SDK ``Tool`` during pure-unit discovery tests."""

    def __init__(
        self,
        name: str,
        description: str | None = "A tool.",
        input_schema: dict | None = None,
        annotations: object | None = None,
        output_schema: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.output_schema = output_schema
        self.annotations = annotations
        self.title = None


class Annotations:
    def __init__(
        self, read_only_hint: bool | None = None, destructive_hint: bool | None = None
    ) -> None:
        self.read_only_hint = read_only_hint
        self.destructive_hint = destructive_hint


class TestQualifiedNames:
    def test_round_trip(self) -> None:
        assert qualify("crm", "get_leads") == f"crm{NAME_SEPARATOR}get_leads"
        assert split_qualified("crm__get_leads") == ("crm", "get_leads")

    def test_tool_names_containing_underscores_survive(self) -> None:
        """A single underscore separator would split ``get_leads`` ambiguously."""
        assert split_qualified(qualify("spreadsheets", "generate_weekly_report")) == (
            "spreadsheets",
            "generate_weekly_report",
        )

    @pytest.mark.parametrize("bad", ["get_leads", "", "__", "crm__", "__get_leads"])
    def test_unqualified_names_are_rejected(self, bad: str) -> None:
        """An unqualified name must never be guessed into a server."""
        with pytest.raises(ValueError):
            split_qualified(bad)

    def test_qualified_names_are_valid_function_identifiers(self) -> None:
        """OpenAI-compatible function names must match ^[a-zA-Z0-9_-]+$."""
        import re

        assert re.fullmatch(r"[a-zA-Z0-9_-]+", qualify("crm", "get_leads"))


class TestDiscovery:
    def test_permission_comes_from_the_local_registry(self) -> None:
        spec = build_tool_spec("email", FakeTool("send_email"))
        assert spec.permission is PermissionLevel.HIGH_RISK

    def test_unknown_tools_are_classified_high_risk(self) -> None:
        spec = build_tool_spec("crm", FakeTool("obliterate_everything"))
        assert spec.permission is PermissionLevel.HIGH_RISK

    def test_a_server_read_only_claim_is_recorded_but_not_obeyed(self) -> None:
        """The conflict is surfaced, and local policy still wins."""
        spec = build_tool_spec(
            "email", FakeTool("send_email", annotations=Annotations(read_only_hint=True))
        )
        assert spec.server_read_only_hint is True
        assert spec.permission is PermissionLevel.HIGH_RISK
        assert spec.hint_conflicts_with_policy

    def test_a_truthful_read_only_hint_is_not_flagged(self) -> None:
        spec = build_tool_spec(
            "crm", FakeTool("get_leads", annotations=Annotations(read_only_hint=True))
        )
        assert not spec.hint_conflicts_with_policy

    def test_a_missing_description_is_substituted(self) -> None:
        spec = build_tool_spec("crm", FakeTool("get_leads", description=None))
        assert "no description" in spec.description


class TestToolCatalog:
    def test_lookup_filtering_and_stats(self) -> None:
        catalog = ToolCatalog(
            [
                make_tool_spec("crm__get_leads", permission=PermissionLevel.READ),
                make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
                make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            ]
        )

        assert len(catalog) == 3
        assert catalog.get("crm__get_leads") is not None
        assert catalog.get("nope") is None
        assert "crm__get_leads" in catalog
        assert catalog.servers == ["crm", "email"]
        assert len(catalog.by_server("crm")) == 2
        assert catalog.stats() == {"read": 1, "write": 1, "high_risk": 1, "total": 3}

    def test_readable_only_removes_everything_that_writes(self) -> None:
        catalog = ToolCatalog(
            [
                make_tool_spec("crm__get_leads", permission=PermissionLevel.READ),
                make_tool_spec("crm__create_lead", permission=PermissionLevel.WRITE),
                make_tool_spec("email__send_email", permission=PermissionLevel.HIGH_RISK),
            ]
        )
        assert [spec.qualified_name for spec in catalog.readable_only()] == ["crm__get_leads"]


class TestToolResults:
    def test_success_and_failure_shapes(self) -> None:
        call = ToolCall(id="1", tool_name="crm__get_leads", arguments={})

        ok = ToolResult.ok(call, content="{}", structured_content={"count": 0}, duration_ms=1.5)
        assert ok.success and ok.server == "crm"
        assert ok.to_payload()["result"] == {"count": 0}

        failed = ToolResult.failure(call, "boom")
        assert not failed.success
        assert failed.to_payload()["error"] == "boom"

    def test_an_unqualified_name_does_not_crash_result_construction(self) -> None:
        """Defensive: a hallucinated name still has to produce a usable result."""
        call = ToolCall(id="1", tool_name="nonsense", arguments={})
        assert ToolResult.failure(call, "unknown").server is None


class TestArgumentValidation:
    SCHEMA = {
        "type": "object",
        "properties": {
            "full_name": {"type": "string", "minLength": 1, "maxLength": 200},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "status": {"$ref": "#/$defs/LeadStatus"},
            "tags": {"type": "array", "minItems": 1},
            "optional": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["full_name"],
        "$defs": {"LeadStatus": {"enum": ["new", "qualified"]}},
    }

    def test_valid_arguments_pass(self) -> None:
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "score": 50}) == []

    def test_missing_required_argument_is_reported(self) -> None:
        problems = validate_arguments(self.SCHEMA, {"score": 10})
        assert any("full_name" in problem for problem in problems)

    def test_unknown_arguments_are_reported(self) -> None:
        """A model inventing a parameter is worth surfacing."""
        problems = validate_arguments(self.SCHEMA, {"full_name": "Ada", "force_delete": True})
        assert any("force_delete" in problem for problem in problems)

    def test_type_errors_are_reported(self) -> None:
        problems = validate_arguments(self.SCHEMA, {"full_name": "Ada", "score": "fifty"})
        assert any("integer" in problem for problem in problems)

    def test_a_boolean_is_not_an_integer(self) -> None:
        """``bool`` subclasses ``int`` in Python; the schema should not accept it."""
        problems = validate_arguments(self.SCHEMA, {"full_name": "Ada", "score": True})
        assert problems

    def test_range_and_length_bounds(self) -> None:
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "score": 150})
        assert validate_arguments(self.SCHEMA, {"full_name": ""})
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "tags": []})

    def test_enums_behind_a_ref_are_resolved(self) -> None:
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "status": "banana"})
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "status": "qualified"}) == []

    def test_anyof_accepts_either_branch(self) -> None:
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "optional": "text"}) == []
        assert validate_arguments(self.SCHEMA, {"full_name": "Ada", "optional": None}) == []

    def test_an_empty_schema_accepts_anything(self) -> None:
        """Never invent a rejection the server would not make."""
        assert validate_arguments({}, {"whatever": 1}) == []
