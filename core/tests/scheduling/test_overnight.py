"""
Overnight availability blocks.

`end_time <= start_time` now means "ends on the next day", so 22:00 -> 06:00 is a
night shift and 00:00 -> 00:00 is a full 24 hours. Before materialization the
engine compared wall-clock times within one local date and bailed on anything
crossing midnight (`local_start.date() != local_end.date() -> False`), which made
a night shift unrepresentable and a 24/7 profile a lie — it had a nightly gap.

These tests pin the three things that had to become true together:
  * a wrapping rule materializes as ONE absolute UTC interval, anchored on the
    day it starts;
  * a slot and a booking may straddle local midnight;
  * the overlap validator sees collisions across the week boundary, which
    per-weekday sorting structurally cannot.
"""

from datetime import date, datetime, time, timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from rest_framework import serializers

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.availability_window import AvailabilityWindow
from core.models.user_event_skill import UserEventSkill
from core.serializers.availability_profile import AvailabilityProfileSerializer
from core.services import availability, bookings as booking_service
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, UTC

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

MONDAY = date(2027, 3, 15)
TUESDAY = date(2027, 3, 16)


def _rule(weekday, start, end):
    """A validate_rules payload entry (the serializer works on dicts, pre-save)."""
    return {"weekday": weekday, "start_time": start, "end_time": end}


@REQUIRES_PG
class OvernightMaterializationTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30
        )
        self.tz = f.make_timezone()  # UTC, so local == UTC and durations are readable

    def _profile(self, rules, tz=None):
        user = f.make_user(self.tenant)
        UserEventSkill.objects.create(
            tenant=self.tenant, user=user, event_template=self.template
        )
        profile = AvailabilityProfile.objects.create(
            tenant=self.tenant, user=user, timezone=tz or self.tz
        )
        for weekday, start, end in rules:
            AvailabilityRule.objects.create(
                tenant=self.tenant,
                profile=profile,
                weekday=weekday,
                start_time=start,
                end_time=end,
            )
        return user, profile

    def test_overnight_rule_materializes_one_window(self):
        """22:00 -> 06:00 is a single 8h interval, not two same-day fragments."""
        user, profile = self._profile([(0, time(22, 0), time(6, 0))])
        f.materialize(profile, MONDAY, MONDAY)

        windows = list(AvailabilityWindow.objects.filter(profile=profile))
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.local_date, MONDAY)  # anchored on the day it STARTS
        self.assertEqual(w.start_utc, datetime(2027, 3, 15, 22, 0, tzinfo=UTC))
        self.assertEqual(w.end_utc, datetime(2027, 3, 16, 6, 0, tzinfo=UTC))
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=8))

    def test_full_day_rule_is_exactly_24h(self):
        user, profile = self._profile([(0, time(0, 0), time(0, 0))])
        f.materialize(profile, MONDAY, MONDAY)

        w = AvailabilityWindow.objects.get(profile=profile)
        self.assertEqual(w.end_utc - w.start_utc, timedelta(hours=24))

    def test_seven_full_day_rules_have_no_gaps(self):
        """The old 00:00-23:59 default left a one-minute hole every night."""
        user, profile = self._profile(
            [(weekday, time(0, 0), time(0, 0)) for weekday in range(7)]
        )
        f.materialize(profile, MONDAY, MONDAY + timedelta(days=6))

        rows = list(
            AvailabilityWindow.objects.filter(profile=profile)
            .order_by("start_utc")
            .values_list("start_utc", "end_utc")
        )
        merged = availability._merge(rows)
        self.assertEqual(len(merged), 1, "24/7 availability must be one contiguous span")
        self.assertEqual(merged[0][1] - merged[0][0], timedelta(days=7))

    def test_slot_straddling_midnight_is_offered(self):
        user, profile = self._profile([(0, time(22, 0), time(6, 0))])
        f.materialize(profile, MONDAY - timedelta(days=1), MONDAY + timedelta(days=1))
        now = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)

        slots = availability.get_available_slots(
            self.tenant, self.template, MONDAY, MONDAY, target_host_id=user.id, now=now
        )
        starts = {s.start for s in slots}
        # 23:30 + 30min lands at 00:00 the next day — the case the old engine
        # rejected outright.
        self.assertIn(datetime(2027, 3, 15, 23, 30, tzinfo=UTC), starts)
        self.assertIn(datetime(2027, 3, 15, 22, 0, tzinfo=UTC), starts)
        self.assertNotIn(datetime(2027, 3, 15, 21, 30, tzinfo=UTC), starts)

    def test_slots_after_midnight_belong_to_the_next_local_date(self):
        """The tail of Monday night is offered when Tuesday is requested."""
        user, profile = self._profile([(0, time(22, 0), time(6, 0))])
        f.materialize(profile, MONDAY - timedelta(days=1), MONDAY + timedelta(days=1))
        now = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)

        slots = availability.get_available_slots(
            self.tenant, self.template, TUESDAY, TUESDAY, target_host_id=user.id, now=now
        )
        starts = {s.start for s in slots}
        self.assertIn(datetime(2027, 3, 16, 5, 0, tzinfo=UTC), starts)
        self.assertNotIn(datetime(2027, 3, 16, 6, 0, tzinfo=UTC), starts)  # shift over
        self.assertNotIn(datetime(2027, 3, 15, 23, 30, tzinfo=UTC), starts)  # Monday

    def test_booking_across_midnight_is_accepted(self):
        user, profile = self._profile([(0, time(22, 0), time(6, 0))])
        f.materialize(profile, MONDAY - timedelta(days=1), MONDAY + timedelta(days=1))
        now = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)

        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=datetime(2027, 3, 15, 23, 45, tzinfo=UTC),
            participants=ATTENDEES,
            target_host_id=user.id,
            now=now,
        )
        self.assertEqual(booking.end_time, datetime(2027, 3, 16, 0, 15, tzinfo=UTC))

    def test_split_pair_across_midnight_merges(self):
        """An author may express a night shift as two same-day rules instead of a
        wrap. Containment merges them, so a straddling booking is still allowed."""
        user, profile = self._profile(
            [
                (0, time(22, 0), time(0, 0)),  # Mon 22:00 -> Tue 00:00
                (1, time(0, 0), time(6, 0)),  # Tue 00:00 -> 06:00
            ]
        )
        f.materialize(profile, MONDAY, TUESDAY)
        now = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)

        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=datetime(2027, 3, 15, 23, 50, tzinfo=UTC),
            participants=ATTENDEES,
            target_host_id=user.id,
            now=now,
        )
        self.assertIsNotNone(booking.id)

    def test_booking_outside_the_shift_is_still_refused(self):
        """Overnight support must not become 'anything goes'."""
        user, profile = self._profile([(0, time(22, 0), time(6, 0))])
        f.materialize(profile, MONDAY - timedelta(days=1), MONDAY + timedelta(days=1))
        now = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)

        with self.assertRaises(Exception) as ctx:
            booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=datetime(2027, 3, 15, 12, 0, tzinfo=UTC),  # midday
                participants=ATTENDEES,
                target_host_id=user.id,
                now=now,
            )
        self.assertIn("available", str(ctx.exception).lower())


class CircularWeekValidatorTests(TestCase):
    """Pure serializer-level validation — no DB rows, no tenant needed."""

    def _validate(self, rules):
        return AvailabilityProfileSerializer().validate_rules(rules)

    def test_plain_same_day_overlap_still_rejected(self):
        with self.assertRaises(serializers.ValidationError):
            self._validate(
                [
                    _rule(0, time(9, 0), time(17, 0)),
                    _rule(0, time(16, 0), time(18, 0)),
                ]
            )

    def test_non_overlapping_split_shift_accepted(self):
        rules = [_rule(0, time(9, 0), time(12, 0)), _rule(0, time(13, 0), time(17, 0))]
        self.assertEqual(self._validate(rules), rules)

    def test_overnight_rule_alone_is_accepted(self):
        """This is the case the old per-rule `end > start` check rejected."""
        rules = [_rule(0, time(22, 0), time(6, 0))]
        self.assertEqual(self._validate(rules), rules)

    def test_wrap_collides_with_next_morning(self):
        """Mon 22:00->06:00 overlaps Tue 02:00->09:00 — invisible to per-day sorting."""
        with self.assertRaises(serializers.ValidationError):
            self._validate(
                [
                    _rule(0, time(22, 0), time(6, 0)),
                    _rule(1, time(2, 0), time(9, 0)),
                ]
            )

    def test_wrap_clearing_the_next_morning_is_accepted(self):
        rules = [_rule(0, time(22, 0), time(6, 0)), _rule(1, time(8, 0), time(17, 0))]
        self.assertEqual(self._validate(rules), rules)

    def test_sunday_night_collides_with_monday_morning(self):
        """The week boundary is the case a linear timeline gets wrong."""
        with self.assertRaises(serializers.ValidationError):
            self._validate(
                [
                    _rule(6, time(23, 0), time(7, 0)),
                    _rule(0, time(2, 0), time(10, 0)),
                ]
            )

    def test_seven_full_days_accepted_as_exactly_one_week(self):
        rules = [_rule(weekday, time(0, 0), time(0, 0)) for weekday in range(7)]
        self.assertEqual(self._validate(rules), rules)

    def test_over_specified_week_rejected(self):
        with self.assertRaises(serializers.ValidationError):
            self._validate([_rule(weekday % 7, time(0, 0), time(0, 0)) for weekday in range(8)])
