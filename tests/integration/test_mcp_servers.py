"""MCP server tests, over the real protocol.

Every call goes through genuine MCP: request serialisation, schema validation on the
server, and a real result coming back. The transport is in-memory rather than a
subprocess, which keeps the suite fast without stubbing any part of the protocol.

Calls are issued through :class:`~app.mcp.manager.MCPManager` rather than a bare
``Client``. Not only does that exercise the production connection and routing code,
it also sidesteps a real constraint: the SDK opens an ``anyio`` task group per
connection, and a task group must be exited by the task that entered it. A pytest
fixture that yields from inside ``async with Client(...)`` is finalised on a
different task and blows up in teardown. ``MCPServerConnection`` solves that with a
supervisor task, so the manager is both the realistic and the workable choice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.mcp.manager import MCPManager
from app.mcp.types import ToolCall, ToolResult
from app.repositories.lead_repository import LeadRepository
from app.repositories.task_repository import TaskRepository

pytestmark = pytest.mark.integration


class ServerClient:
    """Calls one server's tools by their short names, through the manager."""

    def __init__(self, manager: MCPManager, server: str) -> None:
        self._manager = manager
        self._server = server

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return await self._manager.call_tool(
            ToolCall(id=f"test_{name}", tool_name=f"{self._server}__{name}", arguments=arguments)
        )

    def tool_specs(self) -> list[Any]:
        """The specs discovered from this server."""
        return self._manager.catalog.by_server(self._server)


def structured(result: ToolResult) -> Any:
    """Structured payload of a successful call."""
    assert result.success, f"expected success, got: {result.error}"
    return result.structured_content


def error_text(result: ToolResult) -> str:
    return result.error or ""


@pytest.fixture
def crm(mcp_manager: MCPManager) -> ServerClient:
    return ServerClient(mcp_manager, "crm")


@pytest.fixture
def tasks(mcp_manager: MCPManager) -> ServerClient:
    return ServerClient(mcp_manager, "tasks")


@pytest.fixture
def sheets(mcp_manager: MCPManager) -> ServerClient:
    return ServerClient(mcp_manager, "spreadsheets")


@pytest.fixture
def calendar(mcp_manager: MCPManager) -> ServerClient:
    return ServerClient(mcp_manager, "calendar")


@pytest.fixture
def email(mcp_manager: MCPManager) -> ServerClient:
    return ServerClient(mcp_manager, "email")


class TestToolDeclarations:
    async def test_tools_publish_descriptions_and_schemas(self, crm: ServerClient) -> None:
        """Discovery is only useful if a model can act on what it finds."""
        specs = {spec.name: spec for spec in crm.tool_specs()}

        assert set(specs) == {
            "get_leads",
            "get_lead",
            "search_leads",
            "create_lead",
            "update_lead_status",
        }
        for spec in specs.values():
            assert spec.description
            assert spec.input_schema["type"] == "object"

        assert specs["create_lead"].input_schema["required"] == ["full_name", "email"]

    async def test_enum_arguments_are_advertised(self, crm: ServerClient) -> None:
        specs = {spec.name: spec for spec in crm.tool_specs()}
        statuses = specs["get_leads"].input_schema["$defs"]["LeadStatus"]["enum"]
        assert "qualified" in statuses


class TestCRMTools:
    async def test_create_then_read_round_trip(
        self, crm: ServerClient, session: AsyncSession
    ) -> None:
        created = structured(
            await crm.call_tool(
                "create_lead",
                {"full_name": "Ada Lovelace", "email": "ada@acme.io", "status": "qualified"},
            )
        )
        assert created["full_name"] == "Ada Lovelace"

        listed = structured(await crm.call_tool("get_leads", {"status": "qualified"}))
        assert listed["count"] == 1
        assert listed["filters_applied"] == {"status": "qualified"}

        assert await LeadRepository(session).get_by_email("ada@acme.io") is not None

    async def test_creating_the_same_email_twice_does_not_duplicate(
        self, crm: ServerClient
    ) -> None:
        """An agent retrying after a timeout must not double the pipeline."""
        first = structured(
            await crm.call_tool("create_lead", {"full_name": "Ada", "email": "ada@acme.io"})
        )
        second = structured(
            await crm.call_tool("create_lead", {"full_name": "Ada L", "email": "ada@acme.io"})
        )
        assert first["id"] == second["id"]

    async def test_search_finds_by_company(self, crm: ServerClient) -> None:
        await crm.call_tool(
            "create_lead", {"full_name": "Ada", "email": "ada@acme.io", "company": "Acme Corp"}
        )
        found = structured(await crm.call_tool("search_leads", {"query": "acme"}))
        assert found["count"] == 1

    async def test_a_missing_lead_returns_an_actionable_error(self, crm: ServerClient) -> None:
        """Anticipated failures reach the model; it should know what to do next."""
        result = await crm.call_tool(
            "get_lead", {"lead_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert not result.success
        assert "No lead exists" in error_text(result)
        assert "search_leads" in error_text(result)

    async def test_invalid_enum_values_are_rejected_by_the_server(self, crm: ServerClient) -> None:
        result = await crm.call_tool("get_leads", {"status": "banana"})
        assert not result.success
        assert "qualified" in error_text(result)

    async def test_out_of_range_values_are_rejected(self, crm: ServerClient) -> None:
        result = await crm.call_tool(
            "create_lead", {"full_name": "A", "email": "a@b.co", "score": 500}
        )
        assert not result.success


class TestTaskTools:
    async def test_overdue_detection(self, tasks: ServerClient) -> None:
        await tasks.call_tool(
            "create_task",
            {"title": "Late", "due_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
        )
        await tasks.call_tool(
            "create_task",
            {"title": "Later", "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
        )

        overdue = structured(await tasks.call_tool("get_overdue_tasks", {}))
        assert overdue["count"] == 1
        assert overdue["tasks"][0]["title"] == "Late"
        assert overdue["tasks"][0]["is_overdue"] is True

    async def test_naive_timestamps_are_accepted_as_utc(self, tasks: ServerClient) -> None:
        """Models routinely omit the timezone offset, so this must not be an error."""
        result = await tasks.call_tool(
            "create_task", {"title": "Naive", "due_at": "2020-01-01T10:00:00"}
        )
        assert result.success

        overdue = structured(await tasks.call_tool("get_overdue_tasks", {}))
        assert overdue["count"] == 1

    async def test_completing_a_task(self, tasks: ServerClient, session: AsyncSession) -> None:
        created = structured(await tasks.call_tool("create_task", {"title": "Do it"}))
        completed = structured(await tasks.call_tool("complete_task", {"task_id": created["id"]}))

        assert completed["status"] == "done"
        assert await TaskRepository(session).count_open() == 0

    async def test_completing_an_unknown_task_reports_clearly(self, tasks: ServerClient) -> None:
        result = await tasks.call_tool(
            "complete_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )
        assert not result.success
        assert "No task exists" in error_text(result)


class TestSpreadsheetTools:
    async def test_totals_are_computed_from_quantity_and_unit_price(
        self, sheets: ServerClient
    ) -> None:
        """The caller must not be able to supply a total that disagrees with the maths."""
        record = structured(
            await sheets.call_tool(
                "add_sales_record",
                {
                    "record_date": "2026-09-07",
                    "customer_name": "Acme",
                    "product": "Pro",
                    "quantity": 3,
                    "unit_amount": "249.99",
                },
            )
        )
        assert record["total_amount"] == "749.97"

    async def test_a_non_numeric_amount_is_rejected(self, sheets: ServerClient) -> None:
        result = await sheets.call_tool(
            "add_sales_record",
            {
                "record_date": "2026-09-07",
                "customer_name": "A",
                "product": "P",
                "quantity": 1,
                "unit_amount": "free",
            },
        )
        assert not result.success
        assert "not a valid decimal" in error_text(result)

    async def test_summary_aggregates_a_period(self, sheets: ServerClient) -> None:
        for day, amount in (("2026-09-01", "100.00"), ("2026-09-02", "200.00")):
            await sheets.call_tool(
                "add_sales_record",
                {
                    "record_date": day,
                    "customer_name": "Acme",
                    "product": "Pro",
                    "quantity": 1,
                    "unit_amount": amount,
                },
            )

        summary = structured(
            await sheets.call_tool(
                "get_sales_summary", {"start_date": "2026-09-01", "end_date": "2026-09-30"}
            )
        )
        assert summary["record_count"] == 2
        assert summary["total_revenue"] == "300.00"

    async def test_weekly_report_handles_having_no_prior_week(self, sheets: ServerClient) -> None:
        """A first trading week has no baseline; that must not divide by zero."""
        report = structured(
            await sheets.call_tool("generate_weekly_report", {"week_of": "2026-09-07"})
        )
        assert report["revenue_change_pct"] is None
        assert "No sales" in report["headline"]


class TestCalendarTools:
    async def test_todays_meetings(self, calendar: ServerClient) -> None:
        now = datetime.now(UTC)
        await calendar.call_tool(
            "create_event",
            {
                "title": "Standup",
                "starts_at": now.replace(hour=9, minute=0).isoformat(),
                "ends_at": now.replace(hour=9, minute=30).isoformat(),
            },
        )
        today = structured(await calendar.call_tool("get_todays_meetings", {}))
        assert today["count"] == 1
        assert today["events"][0]["duration_minutes"] == 30

    async def test_an_inverted_time_range_is_refused(self, calendar: ServerClient) -> None:
        result = await calendar.call_tool(
            "create_event",
            {
                "title": "Backwards",
                "starts_at": "2026-09-08T11:00:00Z",
                "ends_at": "2026-09-08T10:00:00Z",
            },
        )
        assert not result.success
        assert "must end after it starts" in error_text(result)


class TestEmailTools:
    async def test_drafting_sends_nothing(self, email: ServerClient, settings: Settings) -> None:
        draft = structured(
            await email.call_tool(
                "draft_email",
                {"to": ["ada@acme.io"], "subject": "Draft only", "body": "Hello Ada"},
            )
        )
        assert draft["recipient_count"] == 1

        outbox = settings.email_outbox_dir / "outbox.jsonl"
        assert not outbox.exists() or "Draft only" not in outbox.read_text(encoding="utf-8")

    async def test_invalid_addresses_are_rejected(self, email: ServerClient) -> None:
        result = await email.call_tool(
            "draft_email", {"to": ["not-an-address"], "subject": "x", "body": "y"}
        )
        assert not result.success
        assert "invalid email address" in error_text(result)

    async def test_sending_records_to_the_mock_outbox(
        self, email: ServerClient, settings: Settings
    ) -> None:
        """The server itself will send if called -- the gate lives in the API process."""
        result = structured(
            await email.call_tool(
                "send_email", {"to": ["ada@acme.io"], "subject": "Sent", "body": "Hello"}
            )
        )
        assert result["provider"] == "mock"
        assert result["status"] == "sent"

        record = json.loads(
            (settings.email_outbox_dir / "outbox.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        assert record["subject"] == "Sent"

    async def test_the_recipient_limit_is_enforced_at_the_integration_boundary(
        self, email: Any
    ) -> None:
        result = await email.call_tool(
            "draft_email",
            {
                "to": [f"user{index}@example.com" for index in range(80)],
                "subject": "Blast",
                "body": "Hi",
            },
        )
        assert not result.success
        assert "too many recipients" in error_text(result)
