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
        self.agent = make_agent(self.tenant)

    def test_agent_zone_decides_whose_tuesday(self):
        """The caller is talking to the AGENT. They neither know nor care which
        timezone the host sits in, so the host's zone would answer a question
        nobody asked."""
        self.agent.timezone = self.kolkata
        self.agent.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.agent, date(2027, 3, 16), date(2027, 3, 16)
        )
        # 2027-03-16 00:00 IST == 2027-03-15 18:30Z
        self.assertEqual(start, datetime(2027, 3, 15, 18, 30, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 3, 16, 18, 30, tzinfo=UTC))

    def test_falls_back_to_the_tenant_zone(self):
        """An agent with no timezone of its own uses the org's base clock."""
        self.tenant.timezone = self.denver
        self.tenant.save(update_fields=["timezone"])
        self.agent.timezone = None
        self.agent.save(update_fields=["timezone"])

        start, _end = agent_scheduling._day_range_utc(
            self.agent, date(2027, 3, 16), date(2027, 3, 16)
        )
        # 2027-03-16 00:00 MDT == 06:00Z
        self.assertEqual(start, datetime(2027, 3, 16, 6, 0, tzinfo=UTC))

    def test_agent_zone_overrides_the_tenant(self):
        self.tenant.timezone = self.denver
        self.tenant.save(update_fields=["timezone"])
        self.agent.timezone = self.kolkata
        self.agent.save(update_fields=["timezone"])

        self.assertEqual(
            agent_scheduling.conversation_zone_name(self.agent), "Asia/Kolkata"
        )

    def test_end_date_is_inclusive_to_the_caller(self):
        """"Monday to Tuesday" must cover all of Tuesday. Resolving date_to to
        23:59:59 instead of the next midnight silently drops the last second and
        any slot starting in it."""
        self.agent.timezone = self.denver
        self.agent.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.agent, date(2027, 3, 15), date(2027, 3, 16)
        )
        self.assertEqual(end - start, timedelta(days=2))

    def test_dst_transition_day_is_not_24_hours(self):
        """Denver springs forward on 2027-03-14, so that local day is 23 hours.
        Combining a date with a zone before converting is what gets this right."""
        self.agent.timezone = self.denver
        self.agent.save(update_fields=["timezone"])

        start, end = agent_scheduling._day_range_utc(
            self.agent, date(2027, 3, 14), date(2027, 3, 14)
        )
        self.assertEqual(end - start, timedelta(hours=23))


@REQUIRES_PG
class AgentSpokenTimeTests(TestCase):
    """Two clocks, never mixed.

    The agent stores and passes UTC but speaks the caller's local time. Getting
    this wrong on a voice call is uniquely bad: a wrong time sounds exactly like
    a right one, and nobody finds out until someone misses the meeting.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.kolkata = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30,
            min_notice_minutes=0,
        )
        self.host = f.make_host(self.tenant, self.template)
        self.agent = make_agent(self.tenant, [self.template])
        self.agent.timezone = self.kolkata
        self.agent.save(update_fields=["timezone"])

    def test_slots_carry_utc_and_spoken_forms(self):
        result = agent_scheduling.check_availability(
            self.agent, self.template.id, D, D, target_host_id=self.host.id, now=NOW
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["timezone"], "Asia/Kolkata")
        self.assertTrue(result["slots"])

        slot = result["slots"][0]
        utc_start = datetime.fromisoformat(slot["start"])
        local_start = datetime.fromisoformat(slot["start_local"])

        self.assertEqual(utc_start.utcoffset(), timedelta(0), "`start` must be UTC")
        self.assertEqual(
            local_start.utcoffset(), timedelta(hours=5, minutes=30), "IST offset"
        )
        self.assertEqual(utc_start, local_start, "the two forms are the same instant")

    def test_spoken_string_is_the_local_wall_clock(self):
        """The whole point: 18:30Z must be narrated as midnight IST, not 18:30."""
        zone = agent_scheduling.conversation_zone(self.agent)
        spoken = agent_scheduling._spoken(
            datetime(2027, 3, 15, 18, 30, tzinfo=UTC), zone
        )
        self.assertIn("12:00 AM", spoken)
        self.assertIn("March 16", spoken)  # next day in IST

    def test_spoken_string_has_no_leading_zero_hour(self):
        """'09:00 AM' reads as 'zero nine hundred' in some TTS voices."""
        zone = agent_scheduling.conversation_zone(self.agent)
        spoken = agent_scheduling._spoken(
            datetime(2027, 3, 16, 3, 30, tzinfo=UTC), zone  # 09:00 IST
        )
        self.assertIn("at 9:00 AM", spoken)

    def test_booking_result_reports_both_clocks(self):
        result = agent_scheduling.check_availability(
            self.agent, self.template.id, D, D, target_host_id=self.host.id, now=NOW
        )
        chosen = result["slots"][0]

        booked = agent_scheduling.book(
            self.agent,
            self.template.id,
            datetime.fromisoformat(chosen["start"]),
            participant={"name": "Caller", "email": "caller@example.com"},
            target_host_id=self.host.id,
            now=NOW,
        )
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["timezone"], "Asia/Kolkata")
        self.assertEqual(booked["start"], chosen["start"])
        self.assertEqual(booked["speak"], chosen["speak"])

    def test_agent_without_a_timezone_speaks_utc(self):
        """No agent zone and a UTC tenant: the two forms coincide, and nothing
        raises."""
        plain = make_agent(self.tenant, [self.template], bot_name="Plain")
        self.assertEqual(agent_scheduling.conversation_zone_name(plain), "UTC")

        result = agent_scheduling.check_availability(
            plain, self.template.id, D, D, target_host_id=self.host.id, now=NOW
        )
        slot = result["slots"][0]
        self.assertEqual(
            datetime.fromisoformat(slot["start"]),
            datetime.fromisoformat(slot["start_local"]),
        )


@REQUIRES_PG
class AgentNaiveDatetimeTests(TestCase):
    """A model that echoes back a bare wall-clock time must not be read as UTC.

    Models routinely produce "2027-03-16T15:00:00" after a caller says "three
    o'clock". Read as UTC for an IST-speaking agent, that books 20:30 local —
    five and a half hours off, with a confirmation email that looks correct.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.kolkata = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30,
            min_notice_minutes=0,
        )
        self.host = f.make_host(self.tenant, self.template)
        self.agent = make_agent(self.tenant, [self.template])
        self.agent.timezone = self.kolkata
        self.agent.save(update_fields=["timezone"])

    def test_naive_datetime_is_read_as_the_spoken_zone(self):
        parsed = tools._parse_dt("2027-03-16T15:00:00", self.agent)
        # 15:00 IST == 09:30Z
        self.assertEqual(parsed, datetime(2027, 3, 16, 9, 30, tzinfo=UTC))

    def test_explicit_offset_is_honoured_unchanged(self):
        """Passing a `start` straight back from check_availability must stay exact."""
        parsed = tools._parse_dt("2027-03-16T15:00:00Z", self.agent)
        self.assertEqual(parsed, datetime(2027, 3, 16, 15, 0, tzinfo=UTC))

    def test_a_naive_time_books_the_hour_the_caller_agreed_to(self):
        """End to end: the caller said 3pm, so the booking is 3pm to them."""
        result = tools.call_tool(
            self.agent,
            "book_meeting",
            {
                "event_template_id": str(self.template.id),
                "start_time": "2027-03-16T15:00:00",  # bare wall clock
                "target_host_id": str(self.host.id),
                "participant": {"name": "Caller", "email": "caller@example.com"},
            },
        )
        self.assertTrue(result["ok"], result)

        booking = Booking.objects.get(pk=result["booking_id"])
        spoken_hour = booking.start_time.astimezone(self.kolkata.zone)
        self.assertEqual(spoken_hour.hour, 15)
        self.assertEqual(booking.start_time, datetime(2027, 3, 16, 9, 30, tzinfo=UTC))
