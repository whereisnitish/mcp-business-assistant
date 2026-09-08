"""Repository tests: the SQL that everything else depends on being right."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database.enums import LeadStatus, MessageRole, TaskPriority, TaskStatus
from app.models.database.lead import Lead
from app.models.database.user import User
from app.repositories.base import MAX_PAGE_SIZE, clamp_limit
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.sales_repository import SalesRepository, week_bounds
from app.repositories.task_repository import TaskRepository

pytestmark = pytest.mark.unit


class TestLimits:
    def test_limits_are_clamped_to_a_ceiling(self) -> None:
        """An agent must not be able to pull an entire table into a prompt."""
        assert clamp_limit(10_000) == MAX_PAGE_SIZE
        assert clamp_limit(0) == 1
        assert clamp_limit(-5) == 1
        assert clamp_limit(None) == 25
        assert clamp_limit(50) == 50


class TestCounting:
    async def test_count_is_zero_on_an_empty_table(self, session: AsyncSession) -> None:
        """Regression: rewriting the columns clause drops the FROM and returns 1."""
        assert await LeadRepository(session).count() == 0

    async def test_count_reflects_inserts_and_filters(self, session: AsyncSession) -> None:
        leads = LeadRepository(session)
        await leads.create_lead(full_name="A", email="a@x.com", status=LeadStatus.QUALIFIED)
        await leads.create_lead(full_name="B", email="b@x.com", status=LeadStatus.NEW)

        assert await leads.count() == 2
        filtered = select(Lead).where(Lead.status == LeadStatus.QUALIFIED)
        assert await leads.count(filtered) == 1


class TestLeadRepository:
    async def test_search_matches_across_fields_case_insensitively(
        self, session: AsyncSession
    ) -> None:
        leads = LeadRepository(session)
        await leads.create_lead(full_name="Ada Lovelace", email="ada@acme.io", company="Acme Corp")
        await leads.create_lead(full_name="Bob Stone", email="bob@globex.com", company="Globex")

        assert [lead.full_name for lead in await leads.search("ACME")] == ["Ada Lovelace"]
        assert [lead.full_name for lead in await leads.search("bob@")] == ["Bob Stone"]

    async def test_email_is_normalised_on_create(self, session: AsyncSession) -> None:
        leads = LeadRepository(session)
        created = await leads.create_lead(full_name="Ada", email="  Ada@ACME.io ")
        assert created.email == "ada@acme.io"
        assert await leads.get_by_email("ada@acme.io") is not None

    async def test_status_change_appends_an_auditable_note(self, session: AsyncSession) -> None:
        leads = LeadRepository(session)
        lead = await leads.create_lead(full_name="Ada", email="ada@acme.io")

        await leads.update_status(lead, LeadStatus.QUALIFIED, note="great fit")

        assert lead.status is LeadStatus.QUALIFIED
        assert "[new -> qualified] great fit" in (lead.notes or "")

    async def test_a_malformed_id_returns_none_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        """A model passing nonsense should produce "not found", not a 500."""
        assert await LeadRepository(session).get("definitely-not-a-uuid") is None


class TestTaskRepository:
    async def test_overdue_returns_only_open_past_due_tasks(self, session: AsyncSession) -> None:
        tasks = TaskRepository(session)
        now = datetime.now(UTC)
        overdue = await tasks.create_task(title="Overdue", due_at=now - timedelta(days=2))
        await tasks.create_task(title="Future", due_at=now + timedelta(days=2))
        await tasks.create_task(title="No due date")
        done = await tasks.create_task(title="Done but late", due_at=now - timedelta(days=5))
        await tasks.complete(done)

        titles = [task.title for task in await tasks.list_overdue()]
        assert titles == ["Overdue"]
        assert overdue.is_overdue()

    async def test_completion_is_idempotent(self, session: AsyncSession) -> None:
        tasks = TaskRepository(session)
        task = await tasks.create_task(title="Once", priority=TaskPriority.HIGH)

        first = await tasks.complete(task)
        completed_at = first.completed_at
        second = await tasks.complete(task)

        assert second.status is TaskStatus.DONE
        assert second.completed_at == completed_at

    async def test_undated_tasks_sort_last(self, session: AsyncSession) -> None:
        tasks = TaskRepository(session)
        await tasks.create_task(title="No date")
        await tasks.create_task(title="Soon", due_at=datetime.now(UTC) + timedelta(days=1))

        assert [task.title for task in await tasks.list_tasks()] == ["Soon", "No date"]


class TestSalesRepository:
    async def test_totals_and_breakdowns(self, session: AsyncSession) -> None:
        sales = SalesRepository(session)
        await sales.add_record(
            record_date=date(2026, 9, 1),
            customer_name="Acme",
            product="Pro",
            quantity=2,
            unit_amount=Decimal("100.00"),
            region="EU",
        )
        await sales.add_record(
            record_date=date(2026, 9, 2),
            customer_name="Globex",
            product="Pro",
            quantity=1,
            unit_amount=Decimal("250.50"),
            region="US",
        )

        totals = await sales.totals(start=date(2026, 9, 1), end=date(2026, 9, 30))
        assert totals.record_count == 2
        assert totals.total_revenue == Decimal("450.50")
        assert totals.units_sold == 3
        assert totals.unique_customers == 2

        by_product = await sales.grouped_totals("product")
        assert by_product[0].key == "Pro"
        assert by_product[0].revenue == Decimal("450.50")

    async def test_totals_on_no_data_are_zero_not_null(self, session: AsyncSession) -> None:
        totals = await SalesRepository(session).totals()
        assert totals.record_count == 0
        assert totals.total_revenue == Decimal("0.00")
        assert totals.average_order_value == Decimal("0.00")

    async def test_money_arithmetic_is_exact(self, session: AsyncSession) -> None:
        """Decimal, not float: 0.1 + 0.2 must not become 0.30000000000000004."""
        sales = SalesRepository(session)
        await sales.add_record(
            record_date=date(2026, 9, 1),
            customer_name="A",
            product="P",
            quantity=3,
            unit_amount=Decimal("0.10"),
        )
        totals = await sales.totals()
        assert totals.total_revenue == Decimal("0.30")

    async def test_grouping_is_restricted_to_an_allow_list(self, session: AsyncSession) -> None:
        """The grouping column is reachable from model-proposed arguments."""
        with pytest.raises(ValueError, match="Unsupported grouping"):
            await SalesRepository(session).grouped_totals("record_date; DROP TABLE leads")

    def test_week_bounds_are_monday_to_sunday(self) -> None:
        start, end = week_bounds(date(2026, 9, 9))  # a Wednesday
        assert start == date(2026, 9, 7)
        assert end == date(2026, 9, 13)


class TestCalendarRepository:
    async def test_listing_uses_overlap_not_containment(self, session: AsyncSession) -> None:
        """A meeting already in progress is part of "today's schedule"."""
        calendar = CalendarRepository(session)
        now = datetime.now(UTC)
        await calendar.create_event(
            title="In progress",
            starts_at=now - timedelta(minutes=30),
            ends_at=now + timedelta(minutes=30),
        )
        await calendar.create_event(
            title="Tomorrow",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
        )

        found = await calendar.list_events(start=now, end=now + timedelta(hours=1))
        assert [event.title for event in found] == ["In progress"]

    async def test_duration_is_derived(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        event = await CalendarRepository(session).create_event(
            title="Half hour", starts_at=now, ends_at=now + timedelta(minutes=30)
        )
        assert event.duration_minutes == 30


class TestConversationRepository:
    async def test_message_sequences_are_monotonic(self, session: AsyncSession, user: User) -> None:
        conversations = ConversationRepository(session)
        conversation = await conversations.create_conversation(user_id=user.id)

        for index in range(3):
            await conversations.add_message(
                conversation_id=conversation.id, role=MessageRole.USER, content=f"m{index}"
            )

        messages = await conversations.list_messages(conversation.id)
        assert [message.sequence for message in messages] == [1, 2, 3]
        assert await conversations.next_sequence(conversation.id) == 4

    async def test_history_window_returns_the_newest_turns_in_order(
        self, session: AsyncSession, user: User
    ) -> None:
        """Trim in the database, then present chronologically."""
        conversations = ConversationRepository(session)
        conversation = await conversations.create_conversation(user_id=user.id)
        for index in range(10):
            await conversations.add_message(
                conversation_id=conversation.id, role=MessageRole.USER, content=f"m{index}"
            )

        window = await conversations.list_messages(conversation.id, limit=3)
        assert [message.content for message in window] == ["m7", "m8", "m9"]


class TestUserRepository:
    async def test_api_keys_are_stored_only_as_digests(self, session: AsyncSession) -> None:
        from app.repositories.user_repository import UserRepository

        users = UserRepository(session)
        created = await users.create_user(email="key@example.com", api_key="mba_secret_value")

        assert created.api_key_hash != "mba_secret_value"
        assert await users.get_by_api_key("mba_secret_value") is not None
        assert await users.get_by_api_key("wrong") is None

    async def test_inactive_users_cannot_authenticate(self, session: AsyncSession) -> None:
        from app.repositories.user_repository import UserRepository

        users = UserRepository(session)
        created = await users.create_user(email="off@example.com", api_key="mba_off")
        created.is_active = False
        await session.flush()

        assert await users.get_by_api_key("mba_off") is None
