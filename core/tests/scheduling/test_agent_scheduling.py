"""AI-agent scheduling bridge tests (core/services/agent_scheduling.py).

Exercises the guardrail (agent may only book its linked templates), tenant
isolation, Source.AI tagging, the hold→confirm path, conflict handling, collective
booking, and structured error results. Run on Postgres:
    uv run python manage.py test core.tests.scheduling.test_agent_scheduling \
        --settings=config.settings.test_postgres
"""

from datetime import date, datetime, timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from core.models.agent import Agent
from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.services import agent_scheduling
from core.services import agent_scheduling_tools as tools
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import NOW, UTC, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

PARTICIPANT = {"name": "Customer", "email": "customer@example.com"}
D = date(2027, 3, 16)


def make_agent(tenant, event_templates=(), bot_name="Booker"):
    agent = Agent.objects.create(
        tenant=tenant, bot_name=bot_name, persona_name="Assistant"
    )
    if event_templates:
        agent.event_templates.add(*event_templates)
    return agent


@REQUIRES_PG
class AgentSchedulingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30
        )
        self.host = f.make_host(self.tenant, self.template)
        self.agent = make_agent(self.tenant, [self.template])

    # --- discovery / availability -------------------------------------------
    def test_list_bookable_event_templates(self):
        listed = agent_scheduling.list_bookable_event_templates(self.agent)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], str(self.template.id))
        self.assertEqual(listed[0]["assignment_strategy"], self.template.assignment_strategy)

    def test_check_availability_returns_slots(self):
        result = agent_scheduling.check_availability(
            self.agent, self.template.id, D, D, now=NOW
        )
        self.assertTrue(result["ok"])
        starts = {s["start"] for s in result["slots"]}
        self.assertIn(at(10).isoformat(), starts)

    def test_check_availability_carries_host_ids(self):
        """The model needs a host id to book; slots must supply one."""
        result = agent_scheduling.check_availability(
            self.agent, self.template.id, D, D, now=NOW
        )
        slot = next(s for s in result["slots"] if s["start"] == at(10).isoformat())
        self.assertEqual(slot["host_ids"], [str(self.host.id)])

    def test_list_event_hosts(self):
        result = agent_scheduling.list_event_hosts(self.agent, self.template.id)
        self.assertTrue(result["ok"])
        self.assertEqual([h["id"] for h in result["hosts"]], [str(self.host.id)])

    def test_list_event_hosts_rejects_unlinked_template(self):
        other = f.make_template(self.tenant)
        result = agent_scheduling.list_event_hosts(self.agent, other.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "template_not_allowed")

    # --- booking -------------------------------------------------------------
    def test_book_creates_ai_tagged_booking(self):
        result = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT,
            target_host_id=self.host.id, now=NOW,
        )
        self.assertTrue(result["ok"], result)
        booking = Booking.objects.get(pk=result["booking_id"])
        self.assertEqual(booking.source, Booking.Source.AI)
        self.assertEqual(booking.status, Booking.Status.SCHEDULED)
        self.assertEqual(str(booking.primary_host_id), result["host_id"])

    def test_book_records_participant(self):
        result = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT,
            target_host_id=self.host.id, now=NOW,
        )
        booking = Booking.objects.get(pk=result["booking_id"])
        emails = set(booking.participants.values_list("email", flat=True))
        self.assertIn("customer@example.com", emails)

    # --- guardrails ----------------------------------------------------------
    def test_book_rejects_unlinked_template(self):
        other = f.make_template(self.tenant)  # exists, but NOT linked to the agent
        result = agent_scheduling.book(
            self.agent, other.id, at(10), participant=PARTICIPANT, now=NOW
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "template_not_allowed")
        self.assertFalse(Booking.objects.filter(event_template=other).exists())

    def test_book_rejects_other_tenant_template(self):
        other_tenant = f.make_tenant()
        other_template = f.make_template(other_tenant)
        result = agent_scheduling.book(
            self.agent, other_template.id, at(10), participant=PARTICIPANT, now=NOW
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "template_not_allowed")

    def test_check_availability_rejects_unlinked_template(self):
        other = f.make_template(self.tenant)
        result = agent_scheduling.check_availability(self.agent, other.id, D, D, now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "template_not_allowed")

    # --- hold -> confirm -----------------------------------------------------
    def test_hold_then_confirm(self):
        held = agent_scheduling.hold(
            self.agent, self.template.id, at(10), participant=PARTICIPANT,
            target_host_id=self.host.id, now=NOW,
        )
        self.assertTrue(held["ok"], held)
        booking = Booking.objects.get(pk=held["booking_id"])
        self.assertEqual(booking.status, Booking.Status.HELD)
        self.assertEqual(booking.source, Booking.Source.AI)

        confirmed = agent_scheduling.confirm(self.agent, held["booking_id"], now=NOW)
        self.assertTrue(confirmed["ok"], confirmed)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    def test_double_hold_same_slot_conflicts(self):
        first = agent_scheduling.hold(
            self.agent, self.template.id, at(10),
            target_host_id=self.host.id, now=NOW,
        )
        self.assertTrue(first["ok"])
        second = agent_scheduling.hold(
            self.agent, self.template.id, at(10),
            target_host_id=self.host.id, now=NOW,
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "slot_taken")

    # --- cancel --------------------------------------------------------------
    def test_cancel_booking(self):
        booked = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT,
            target_host_id=self.host.id, now=NOW,
        )
        result = agent_scheduling.cancel(self.agent, booked["booking_id"], now=NOW)
        self.assertTrue(result["ok"], result)
        booking = Booking.objects.get(pk=booked["booking_id"])
        self.assertEqual(booking.status, Booking.Status.CANCELED)

    def test_cancel_unknown_booking(self):
        import uuid

        result = agent_scheduling.cancel(self.agent, uuid.uuid4(), now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")

    # --- host is mandatory, and the error tells the model what to do ---------
    def test_book_without_host_is_structured_error(self):
        result = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT, now=NOW
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "host_required")
        self.assertFalse(Booking.objects.exists())

    def test_hold_without_host_is_structured_error(self):
        result = agent_scheduling.hold(
            self.agent, self.template.id, at(10), participant=PARTICIPANT, now=NOW
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "host_required")

    # --- policy errors are structured, not raised ----------------------------
    def test_book_in_the_past_is_policy_violation(self):
        # now is AFTER the requested start -> past booking.
        result = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT,
            target_host_id=self.host.id, now=at(11),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "policy_violation")
        self.assertEqual(result["field"], "start_time")


@REQUIRES_PG
class AgentCollectiveSchedulingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant,
            assignment_strategy=EventTemplate.AssignmentStrategy.COLLECTIVE,
            duration_minutes=30,
        )
        self.host_a = f.make_host(self.tenant, self.template)
        self.host_b = f.make_host(self.tenant, self.template)
        self.agent = make_agent(self.tenant, [self.template])

    def test_book_collective_books_every_host(self):
        result = agent_scheduling.book(
            self.agent, self.template.id, at(10), participant=PARTICIPANT, now=NOW
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["group"])
        self.assertEqual(result["count"], 2)
        group_bookings = Booking.objects.filter(group_id=result["group_id"])
        self.assertEqual(group_bookings.count(), 2)
        self.assertTrue(all(b.source == Booking.Source.AI for b in group_bookings))

    def test_hold_not_supported_for_collective(self):
        result = agent_scheduling.hold(self.agent, self.template.id, at(10), now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "collective_no_hold")


@REQUIRES_PG
class AgentToolDispatchTests(TestCase):
    """The LLM tool layer (agent_scheduling_tools) parses args and routes to the
    guardrailed service."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30
        )
        self.host = f.make_host(self.tenant, self.template)
        self.agent = make_agent(self.tenant, [self.template])

    def test_schema_covers_all_tools(self):
        names = {t["name"] for t in tools.TOOL_SCHEMAS}
        self.assertEqual(
            names,
            {
                "list_event_types",
                "list_event_hosts",
                "check_availability",
                "book_meeting",
                "hold_slot",
                "confirm_booking",
                "cancel_booking",
            },
        )

    def test_book_meeting_tool_parses_iso_and_books(self):
        result = tools.call_tool(
            self.agent,
            "book_meeting",
            {
                "event_template_id": str(self.template.id),
                "start_time": at(10).isoformat().replace("+00:00", "Z"),
                "participant": {"name": "Customer", "email": "customer@example.com"},
                "target_host_id": str(self.host.id),
            },
        )
        self.assertTrue(result["ok"], result)
        booking = Booking.objects.get(pk=result["booking_id"])
        self.assertEqual(booking.source, Booking.Source.AI)

    def test_check_availability_tool_parses_dates(self):
        result = tools.call_tool(
            self.agent,
            "check_availability",
            {
                "event_template_id": str(self.template.id),
                "date_from": "2027-03-16",
                "date_to": "2027-03-16",
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertIsInstance(result["slots"], list)

    def test_unknown_tool(self):
        result = tools.call_tool(self.agent, "delete_everything", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_tool")

    def test_bad_arguments(self):
        result = tools.call_tool(self.agent, "book_meeting", {})  # missing required keys
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_arguments")


@REQUIRES_PG
class AgentDateResolutionTests(TestCase):
    """The AI bridge is the one caller that still speaks in dates — a person on the
    phone says "Tuesday", not an ISO instant. So it resolves the date to UTC
    instants itself, and whose day it means is a decision, not an accident.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.kolkata = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.denver = f.make_timezone("America/Denver", "UTC-07:00", "Denver")

    def test_named_host_zone_wins(self):
        """The caller is asking about that host's working day."""
        template = f.make_template(self.tenant)
        host = f.make_host(self.tenant, template, tz=self.kolkata)

        start, end = agent_scheduling._day_range_utc(
            self.tenant, date(2027, 3, 16), date(2027, 3, 16), target_host_id=host.id
        )
        # 2027-03-16 00:00 IST == 2027-03-15 18:30Z
        self.assertEqual(start, datetime(2027, 3, 15, 18, 30, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 3, 16, 18, 30, tzinfo=UTC))

    def test_falls_back_to_the_tenant_zone(self):
        """'When is anyone free on Tuesday?' — no host named, so the tenant's base
        timezone is the closest thing to a house clock."""
        self.tenant.timezone = self.denver
        self.tenant.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.tenant, date(2027, 3, 16), date(2027, 3, 16)
        )
        # 2027-03-16 00:00 MDT == 06:00Z
        self.assertEqual(start, datetime(2027, 3, 16, 6, 0, tzinfo=UTC))

    def test_end_date_is_inclusive_to_the_caller(self):
        """"Monday to Tuesday" must cover all of Tuesday. Resolving date_to to
        23:59:59 instead of the next midnight silently drops the last second and
        any slot starting in it."""
        self.tenant.timezone = self.denver
        self.tenant.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.tenant, date(2027, 3, 15), date(2027, 3, 16)
        )
        self.assertEqual(end - start, timedelta(days=2))

    def test_dst_transition_day_is_not_24_hours(self):
        """Denver springs forward on 2027-03-14, so that local day is 23 hours.
        Combining a date with a zone before converting is what gets this right."""
        self.tenant.timezone = self.denver
        self.tenant.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.tenant, date(2027, 3, 14), date(2027, 3, 14)
        )
        self.assertEqual(end - start, timedelta(hours=23))
