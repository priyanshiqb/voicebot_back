"""
DST behaviour of materialization, at the level that matters operationally: what
lands in AvailabilityWindow.

Timezone.to_utc() is unit-tested in onboard/tests/test_timezone_conversion.py.
This file asserts the materializer applies it with the right fold on each end and
handles the one case that cannot produce a row at all.

All dates are fixed, never derived from "now", so these do not rot:
  * 2026-03-08  America/New_York springs forward, 02:00 -> 03:00 (02:00-02:59 gap)
  * 2026-11-01  America/New_York falls back,      01:00-01:59 happens twice
  * 2026-03-29  Europe/London springs forward,    01:00 -> 02:00
"""

from datetime import date, datetime, time, timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.availability_window import AvailabilityWindow
from core.services import availability_materializer as materializer
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import UTC

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

SPRING_FORWARD_NY = date(2026, 3, 8)  # Sunday, weekday 6
FALL_BACK_NY = date(2026, 11, 1)  # Sunday, weekday 6
SPRING_FORWARD_LONDON = date(2026, 3, 29)  # Sunday, weekday 6
SUNDAY = 6


@REQUIRES_PG
class DstMaterializationTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.ny = f.make_timezone("America/New_York", "UTC-05:00", "New York")
        self.london = f.make_timezone("Europe/London", "UTC+00:00", "London")

    def _profile(self, tz, weekday, start, end):
        user = f.make_user(self.tenant)
        profile = AvailabilityProfile.objects.create(
            tenant=self.tenant, user=user, timezone=tz
        )
        AvailabilityRule.objects.create(
            tenant=self.tenant,
            profile=profile,
            weekday=weekday,
            start_time=start,
            end_time=end,
        )
        return profile

    def _window(self, profile, day):
        return AvailabilityWindow.objects.filter(profile=profile, local_date=day).first()

    # --- spring forward -------------------------------------------------------

    def test_nonexistent_start_snaps_to_the_transition(self):
        """02:30 does not exist on 2026-03-08 in New York; 03:00 EDT = 07:00Z is
        the first instant that does."""
        profile = self._profile(self.ny, SUNDAY, time(2, 30), time(10, 0))
        materializer.materialize_profile(profile, SPRING_FORWARD_NY, SPRING_FORWARD_NY)

        w = self._window(profile, SPRING_FORWARD_NY)
        self.assertEqual(w.start_utc, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))
        self.assertEqual(w.dst_flag, "nonexistent")

    def test_gap_start_snaps_to_transition_not_gap_width(self):
        """Regression. 02:30 -> 03:30 must be a 30-MINUTE window.

        Shifting a nonexistent time forward by the gap WIDTH (what `fold=0` and
        dateutil's resolve_imaginary both do) sends 02:30 to 03:30 — the same
        instant as the end — producing a zero-length window that the
        end_utc > start_utc check constraint then rejects, aborting the whole
        profile's materialization.
        """
        profile = self._profile(self.ny, SUNDAY, time(2, 30), time(3, 30))
        materializer.materialize_profile(profile, SPRING_FORWARD_NY, SPRING_FORWARD_NY)

        w = self._window(profile, SPRING_FORWARD_NY)
        self.assertIsNotNone(w, "the window was skipped as zero-length")
        self.assertEqual(w.end_utc - w.start_utc, timedelta(minutes=30))
        self.assertEqual(w.start_utc, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))

    def test_block_wholly_inside_the_gap_emits_no_window(self):
        """02:00 -> 03:00 is exactly the hour that does not happen."""
        profile = self._profile(self.ny, SUNDAY, time(2, 0), time(3, 0))
        with self.assertLogs("core.services.availability_materializer", level="INFO") as logs:
            written = materializer.materialize_profile(
                profile, SPRING_FORWARD_NY, SPRING_FORWARD_NY
            )

        self.assertEqual(written, 0)
        self.assertIsNone(self._window(profile, SPRING_FORWARD_NY))
        self.assertTrue(any("DST-gap" in line for line in logs.output))

    def test_gap_skip_does_not_affect_neighbouring_weeks(self):
        """The skip is one date, not the rule."""
        profile = self._profile(self.ny, SUNDAY, time(2, 0), time(3, 0))
        materializer.materialize_profile(
            profile, SPRING_FORWARD_NY - timedelta(days=7), SPRING_FORWARD_NY + timedelta(days=7)
        )

        days = set(
            AvailabilityWindow.objects.filter(profile=profile).values_list(
                "local_date", flat=True
            )
        )
        self.assertEqual(
            days,
            {SPRING_FORWARD_NY - timedelta(days=7), SPRING_FORWARD_NY + timedelta(days=7)},
        )

    def test_overnight_across_spring_forward_is_seven_hours(self):
        """22:00 -> 06:00 loses an hour of real time on the transition night."""
        profile = self._profile(self.ny, SUNDAY, time(22, 0), time(6, 0))
        # The Saturday BEFORE the transition is weekday 5, so anchor the rule on
        # the Saturday night that runs into the spring-forward Sunday.
        saturday = SPRING_FORWARD_NY - timedelta(days=1)
        AvailabilityRule.objects.filter(profile=profile).update(weekday=saturday.weekday())
        materializer.materialize_profile(profile, saturday, saturday)

        w = self._window(profile, saturday)
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=7))

    def test_london_spring_forward_is_handled_too(self):
        """Not a New-York-only code path — the gap is at 01:00 local here."""
        profile = self._profile(self.london, SUNDAY, time(1, 30), time(9, 0))
        materializer.materialize_profile(
            profile, SPRING_FORWARD_LONDON, SPRING_FORWARD_LONDON
        )

        w = self._window(profile, SPRING_FORWARD_LONDON)
        self.assertEqual(w.dst_flag, "nonexistent")
        self.assertEqual(w.start_utc, datetime(2026, 3, 29, 1, 0, tzinfo=UTC))

    # --- fall back ------------------------------------------------------------

    def test_overnight_across_fall_back_is_nine_hours(self):
        """The same 22:00 -> 06:00 rule gains an hour in autumn."""
        profile = self._profile(self.ny, SUNDAY, time(22, 0), time(6, 0))
        saturday = FALL_BACK_NY - timedelta(days=1)
        AvailabilityRule.objects.filter(profile=profile).update(weekday=saturday.weekday())
        materializer.materialize_profile(profile, saturday, saturday)

        w = self._window(profile, saturday)
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=9))

    def test_ambiguous_end_takes_the_second_occurrence(self):
        """01:30 happens twice on 2026-11-01. A block ENDING there must cover both,
        or the host loses an hour they are actually working."""
        profile = self._profile(self.ny, SUNDAY, time(22, 0), time(1, 30))
        saturday = FALL_BACK_NY - timedelta(days=1)
        AvailabilityRule.objects.filter(profile=profile).update(weekday=saturday.weekday())
        materializer.materialize_profile(profile, saturday, saturday)

        w = self._window(profile, saturday)
        self.assertEqual(w.dst_flag, "ambiguous")
        # 01:30 EST (fold=1) = 06:30Z. The first occurrence, 01:30 EDT, is 05:30Z.
        self.assertEqual(w.end_utc, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=4, minutes=30))

    def test_ambiguous_start_takes_the_first_occurrence(self):
        """A block STARTING in the repeated hour opens at its first occurrence."""
        profile = self._profile(self.ny, SUNDAY, time(1, 30), time(9, 0))
        materializer.materialize_profile(profile, FALL_BACK_NY, FALL_BACK_NY)

        w = self._window(profile, FALL_BACK_NY)
        self.assertEqual(w.dst_flag, "ambiguous")
        self.assertEqual(w.start_utc, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # EDT

    # --- the ordinary case ----------------------------------------------------

    def test_clear_boundaries_across_a_transition_need_no_policy(self):
        """A block whose ENDS are both unambiguous just works, and carries no flag —
        the transition inside it falls out of the UTC arithmetic."""
        profile = self._profile(self.ny, SUNDAY, time(9, 0), time(17, 0))
        materializer.materialize_profile(profile, SPRING_FORWARD_NY, SPRING_FORWARD_NY)

        w = self._window(profile, SPRING_FORWARD_NY)
        self.assertEqual(w.dst_flag, "")
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=8))
        # 09:00 is after the 02:00 transition, so it is already EDT (-04:00).
        self.assertEqual(w.start_utc, datetime(2026, 3, 8, 13, 0, tzinfo=UTC))

    def test_same_wall_clock_rule_shifts_utc_across_the_year(self):
        """09:00 local is 14:00Z in winter and 13:00Z in summer. One stored UTC
        time would be wrong for half the year — this is why authoring stays
        wall-clock and only the windows are UTC."""
        profile = self._profile(self.ny, SUNDAY, time(9, 0), time(17, 0))
        winter, summer = date(2026, 1, 11), date(2026, 7, 12)
        materializer.materialize_profile(profile, winter, winter)
        materializer.materialize_profile(profile, summer, summer)

        self.assertEqual(self._window(profile, winter).start_utc.hour, 14)  # EST
        self.assertEqual(self._window(profile, summer).start_utc.hour, 13)  # EDT
