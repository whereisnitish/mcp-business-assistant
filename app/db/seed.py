"""Demo data seeding -- development and evaluation only.

Enabled with ``SEED_DEMO_DATA=true`` (the default in ``docker compose``). Without
it a reviewer's first request would truthfully answer "you have no leads", which
demonstrates nothing.

Seeding is **idempotent by emptiness**: it inserts only when the relevant table is
empty, so restarting the container does not accumulate duplicate leads, and it will
never touch a database that already holds real data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, safe_extra
from app.db.session import session_scope
from app.models.database.enums import LeadStatus, TaskPriority, UserRole
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.sales_repository import SalesRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

DEMO_LEADS = [
    (
        "Ada Lovelace",
        "ada@analytical.io",
        "Analytical Engines",
        "webinar",
        LeadStatus.QUALIFIED,
        92,
        48000,
    ),
    ("Grace Hopper", "grace@navsys.mil", "NavSys", "referral", LeadStatus.QUALIFIED, 88, 65000),
    (
        "Alan Turing",
        "alan@enigma.co.uk",
        "Enigma Consulting",
        "conference",
        LeadStatus.CONTACTED,
        71,
        22000,
    ),
    (
        "Katherine Johnson",
        "katherine@orbital.space",
        "Orbital Dynamics",
        "inbound",
        LeadStatus.NEW,
        55,
        15000,
    ),
    (
        "Margaret Hamilton",
        "margaret@apollo.dev",
        "Apollo Software",
        "webinar",
        LeadStatus.WON,
        95,
        120000,
    ),
    (
        "Barbara Liskov",
        "barbara@substitution.io",
        "Substitution Labs",
        "outbound",
        LeadStatus.NEW,
        40,
        8000,
    ),
]

DEMO_SALES = [
    ("Apollo Software", "Enterprise Plan", 3, Decimal("4000.00"), "NA"),
    ("Analytical Engines", "Pro Plan", 5, Decimal("1200.00"), "EU"),
    ("NavSys", "Enterprise Plan", 1, Decimal("4000.00"), "NA"),
    ("Enigma Consulting", "Starter Plan", 10, Decimal("250.00"), "EU"),
    ("Orbital Dynamics", "Pro Plan", 2, Decimal("1200.00"), "APAC"),
]


async def seed_demo_data(settings: Settings | None = None) -> bool:
    """Insert illustrative business data when the database is empty.

    Returns True when data was written, False when it was skipped.
    """
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        leads_repo = LeadRepository(session)
        if await leads_repo.count() > 0:
            logger.info("demo seed skipped; data already present", extra={"event": "seed.skipped"})
            return False

        users = UserRepository(session)
        owner = await users.get_or_create(
            email=settings.demo_user_email, full_name="Demo User", role=UserRole.OPERATOR
        )

        created_leads = []
        for name, email, company, source, lead_status, score, value in DEMO_LEADS:
            created_leads.append(
                await leads_repo.create_lead(
                    full_name=name,
                    email=email,
                    company=company,
                    source=source,
                    status=lead_status,
                    score=score,
                    estimated_value=value,
                    owner_id=owner.id,
                )
            )

        now = datetime.now(UTC)
        tasks = TaskRepository(session)
        # A mix of overdue, due-soon and undated work, so "what's overdue?" has a
        # non-trivial answer rather than returning everything or nothing.
        await tasks.create_task(
            title="Send pricing to Ada Lovelace",
            description="She asked for enterprise pricing after the webinar.",
            due_at=now - timedelta(days=3),
            priority=TaskPriority.HIGH,
            lead_id=created_leads[0].id,
            assignee_id=owner.id,
        )
        await tasks.create_task(
            title="Follow up with Alan Turing",
            due_at=now - timedelta(days=1),
            priority=TaskPriority.URGENT,
            lead_id=created_leads[2].id,
            assignee_id=owner.id,
        )
        await tasks.create_task(
            title="Prepare Q4 renewal deck for Apollo Software",
            due_at=now + timedelta(days=5),
            priority=TaskPriority.MEDIUM,
            lead_id=created_leads[4].id,
            assignee_id=owner.id,
        )
        await tasks.create_task(
            title="Review lead scoring rules", priority=TaskPriority.LOW, assignee_id=owner.id
        )

        sales = SalesRepository(session)
        for index, (customer, product, quantity, unit, region) in enumerate(DEMO_SALES):
            await sales.add_record(
                record_date=(now - timedelta(days=index * 2)).date(),
                customer_name=customer,
                product=product,
                quantity=quantity,
                unit_amount=unit,
                region=region,
                sales_rep="Demo User",
            )
        # A few sales in the previous week so the weekly report has a baseline to
        # compare against instead of reporting "no prior week".
        for index, (customer, product, quantity, unit, region) in enumerate(DEMO_SALES[:3]):
            await sales.add_record(
                record_date=(now - timedelta(days=9 + index)).date(),
                customer_name=customer,
                product=product,
                quantity=max(1, quantity - 1),
                unit_amount=unit,
                region=region,
                sales_rep="Demo User",
            )

        calendar = CalendarRepository(session)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        await calendar.create_event(
            title="Pipeline review",
            starts_at=start_of_today + timedelta(hours=9),
            ends_at=start_of_today + timedelta(hours=9, minutes=30),
            location="Zoom",
            attendees=["demo@example.com"],
            owner_id=owner.id,
        )
        await calendar.create_event(
            title="Demo call with Grace Hopper",
            starts_at=start_of_today + timedelta(hours=14),
            ends_at=start_of_today + timedelta(hours=15),
            location="Google Meet",
            attendees=["grace@navsys.mil", "demo@example.com"],
            owner_id=owner.id,
            lead_id=created_leads[1].id,
        )
        await calendar.create_event(
            title="Contract review with Apollo Software",
            starts_at=start_of_today + timedelta(days=1, hours=11),
            ends_at=start_of_today + timedelta(days=1, hours=12),
            attendees=["margaret@apollo.dev"],
            owner_id=owner.id,
        )

    logger.info(
        "demo data seeded",
        extra=safe_extra(
            {"event": "seed.completed", "leads": len(DEMO_LEADS), "sales": len(DEMO_SALES) + 3}
        ),
    )
    return True
