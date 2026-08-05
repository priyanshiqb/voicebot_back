"""
The available-slots wire contract.

Two halves, and they are deliberately different:

  * OUT — every instant is UTC, ISO-8601, `Z`-suffixed. The frontend renders it in
    whatever zone the user selected.
  * IN  — `start_utc` / `end_utc` are instants too, NOT dates. A date is a
    different 24 hours in every timezone, so a date range forces the server to
    guess whose day the caller meant. The caller is the only component that knows
    which zone the human picked, so it resolves the day and sends instants.

The `CustomerTimezoneTests` class at the bottom is the concrete bug this design
fixes: with a date range, a customer in New York booking a host in Kolkata got
slots from the wrong day and missed slots on their own.
"""

from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import skipUnless
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.user_event_skill import UserEventSkill
from core.services import availability
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import UTC

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

SLOTS_URL = "/api/v1/core/availability/available-slots/"
MONDAY = date(2027, 3, 15)


@REQUIRES_PG
class RangeBoundaryTests(TestCase):
    """`[range_start, range_end)` — half-open, so consecutive queries tile."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=60, slot_granularity_minutes=60,
            min_notice_minutes=0,
        )
        self.host = f.make_host(self.tenant, self.template)  # 24/7, UTC
        self.now = datetime(2027, 3, 14, 0, 0, tzinfo=UTC)

    def _starts(self, start, end):
        return [
            s.start
            for s in availability.get_available_slots(
                self.tenant, self.template, start, end,
                target_host_id=self.host.id, now=self.now,
            )
        ]

    def test_start_is_inclusive_and_end_is_exclusive(self):
        start = datetime(2027, 3, 15, 9, 0, tzinfo=UTC)
        end = datetime(2027, 3, 15, 12, 0, tzinfo=UTC)
        starts = self._starts(start, end)

        self.assertIn(start, starts, "range_start must be inclusive")
        self.assertIn(datetime(2027, 3, 15, 11, 0, tzinfo=UTC), starts)
        self.assertNotIn(end, starts, "range_end must be exclusive")

    def test_consecutive_ranges_tile_without_gap_or_overlap(self):
        """The practical payoff of half-open bounds: paging day by day neither
        duplicates a slot nor drops one."""
        day_one = f.utc_day(MONDAY)
        day_two = f.utc_day(MONDAY + timedelta(days=1))

        first, second = self._starts(*day_one), self._starts(*day_two)
        self.assertTrue(first and second)
        self.assertEqual(set(first) & set(second), set(), "a slot appeared in both pages")

        merged = self._starts(day_one[0], day_two[1])
        self.assertEqual(sorted(first + second), sorted(merged), "paging lost a slot")

    def test_naive_bounds_are_read_as_utc(self):
        """Django's own parse helpers can yield naive values; comparing one against
        the aware instants in AvailabilityWindow is a TypeError, i.e. a 500."""
        naive = self._starts(
            datetime(2027, 3, 15, 9, 0), datetime(2027, 3, 15, 12, 0)
        )
        aware = self._starts(
            datetime(2027, 3, 15, 9, 0, tzinfo=UTC),
            datetime(2027, 3, 15, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(naive, aware)

    def test_non_utc_bounds_are_converted_not_rejected(self):
        kolkata = ZoneInfo("Asia/Kolkata")
        via_ist = self._starts(
            datetime(2027, 3, 15, 14, 30, tzinfo=kolkata),  # 09:00Z
            datetime(2027, 3, 15, 17, 30, tzinfo=kolkata),  # 12:00Z
        )
        via_utc = self._starts(
            datetime(2027, 3, 15, 9, 0, tzinfo=UTC),
            datetime(2027, 3, 15, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(via_ist, via_utc)

    def test_a_date_is_rejected_outright(self):
        """Silently reading a date as UTC midnight is the exact ambiguity this
        signature exists to remove, so it must fail loudly at the boundary."""
        with self.assertRaises(TypeError) as ctx:
            self._starts(MONDAY, MONDAY + timedelta(days=1))
        self.assertIn("aware UTC datetime", str(ctx.exception))


@REQUIRES_PG
class ResponseFormatTests(APITestCase):
    """Nothing else pins this: a DRF settings change could silently move the
    format and every client's parsing would shift with it."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(
            self.tenant, duration_minutes=60, slot_granularity_minutes=60,
            min_notice_minutes=0,
        )
        self.host = f.make_host(self.tenant, self.template)
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def test_slot_instants_are_utc_iso8601_with_z(self):
        start, end = f.utc_day(MONDAY)
        response = self.client.get(SLOTS_URL, {
            "event_template": str(self.template.id),
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "target_host": str(self.host.id),
        })
        self.assertEqual(response.status_code, 200, response.data)

        rendered = response.json()["data"]
        self.assertTrue(rendered, "no slots to check the format of")
        for slot in rendered[:5]:
            for field in ("start", "end"):
                value = slot[field]
                self.assertTrue(
                    value.endswith("Z"),
                    f"{field}={value!r} is not UTC-suffixed; a client parsing it as "
                    f"local time would be off by its own offset",
                )
                # Round-trips to an aware UTC instant.
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                self.assertEqual(parsed.utcoffset(), timedelta(0))


@REQUIRES_PG
class CustomerTimezoneTests(TestCase):
    """The bug that motivated the change.

    Host in Kolkata (+05:30) working 09:00-12:00 and an overnight 22:00->02:00.
    Customer's UI is set to New York (-04:00 in August). They pick August 8th.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.tz = f.make_timezone("Asia/Kolkata", "UTC+05:30", "Kolkata")
        self.template = f.make_template(
            self.tenant, duration_minutes=60, slot_granularity_minutes=60,
            min_notice_minutes=0,
        )
        self.host = f.make_user(self.tenant)
        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.host, event_template=self.template
        )
        profile = AvailabilityProfile.objects.create(
            tenant=self.tenant, user=self.host, timezone=self.tz
        )
        for weekday in range(7):
            for start, end in ((time(9, 0), time(12, 0)), (time(22, 0), time(2, 0))):
                AvailabilityRule.objects.create(
                    tenant=self.tenant, profile=profile, weekday=weekday,
                    start_time=start, end_time=end,
                )
        f.materialize(profile, date(2026, 8, 1), date(2026, 8, 20))
        self.now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        self.ny = ZoneInfo("America/New_York")

    def _starts(self, start, end):
        return [
            s.start
            for s in availability.get_available_slots(
                self.tenant, self.template, start, end,
                target_host_id=self.host.id, now=self.now,
            )
        ]

    def test_customers_day_is_honoured_exactly(self):
        """Every returned slot falls on Aug 8 in the CUSTOMER's zone — no slot from
        their Aug 7, and none of their Aug 8 missing."""
        start, end = f.local_day(date(2026, 8, 8), self.ny)
        self.assertEqual(start, datetime(2026, 8, 8, 4, 0, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 8, 9, 4, 0, tzinfo=UTC))

        starts = self._starts(start, end)
        self.assertTrue(starts)
        for slot_start in starts:
            self.assertEqual(
                slot_start.astimezone(self.ny).date(),
                date(2026, 8, 8),
                f"{slot_start} is not on the customer's Aug 8",
            )

    def test_the_late_evening_slots_a_date_range_used_to_drop(self):
        """22:00 and 23:00 Kolkata on Aug 8 are 16:30Z / 17:30Z — the customer's
        Aug 8 lunchtime. 00:00 and 01:00 Kolkata on Aug 9 are 18:30Z / 19:30Z —
        still the customer's Aug 8 afternoon, and previously unreachable without
        querying Aug 9."""
        start, end = f.local_day(date(2026, 8, 8), self.ny)
        starts = set(self._starts(start, end))

        self.assertIn(datetime(2026, 8, 8, 16, 30, tzinfo=UTC), starts)
        self.assertIn(datetime(2026, 8, 8, 18, 30, tzinfo=UTC), starts)
        self.assertIn(datetime(2026, 8, 8, 19, 30, tzinfo=UTC), starts)

    def test_the_previous_days_slots_a_date_range_used_to_include(self):
        """18:30Z on Aug 7 is Kolkata Aug 8 00:00 — but New York Aug 7 14:30. It
        must NOT appear in the customer's Aug 8."""
        start, end = f.local_day(date(2026, 8, 8), self.ny)
        starts = set(self._starts(start, end))

        self.assertNotIn(datetime(2026, 8, 7, 18, 30, tzinfo=UTC), starts)
        self.assertNotIn(datetime(2026, 8, 8, 3, 30, tzinfo=UTC), starts)  # NY Aug 7 23:30

    def test_the_hosts_own_day_still_works(self):
        """The host's local day is just another range — nothing privileges UTC."""
        start, end = f.local_day(date(2026, 8, 8), self.tz)
        for slot_start in self._starts(start, end):
            self.assertEqual(
                slot_start.astimezone(self.tz.zone).date(), date(2026, 8, 8)
            )
