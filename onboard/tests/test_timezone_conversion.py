"""
Tests for `Timezone` as the codebase's wall-clock <-> UTC conversion authority.

Every date here is FIXED and chosen for a specific DST property — never `now()`.
2026 transitions used:
    America/New_York   spring 2026-03-08 02:00->03:00,  fall 2026-11-01 02:00->01:00
    Europe/London      spring 2026-03-29 01:00Z,        fall 2026-10-25
    Australia/Sydney   fall   2026-04-05,               spring 2026-10-04
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from onboard.models.timezone import Timezone, validate_iana_timezone

UTC = ZoneInfo("UTC")


def tz(name):
    """An unsaved Timezone — `zone` resolves from the string, so no DB needed."""
    return Timezone(timezone=name, offset="", region="")


class OffsetQueryTests(SimpleTestCase):
    def test_utc_offset_at_tracks_dst(self):
        ny = tz("America/New_York")
        self.assertEqual(
            ny.utc_offset_at(datetime(2026, 1, 15, 12, tzinfo=UTC)), timedelta(hours=-5)
        )
        self.assertEqual(
            ny.utc_offset_at(datetime(2026, 7, 15, 12, tzinfo=UTC)), timedelta(hours=-4)
        )

    def test_offset_label_at_formats(self):
        jan = datetime(2026, 1, 15, 12, tzinfo=UTC)
        jul = datetime(2026, 7, 15, 12, tzinfo=UTC)

        # Half-hour and three-quarter-hour zones must not lose their minutes.
        self.assertEqual(tz("Asia/Kolkata").offset_label_at(jan), "UTC+05:30")
        self.assertEqual(tz("Asia/Kathmandu").offset_label_at(jan), "UTC+05:45")
        self.assertEqual(tz("Pacific/Chatham").offset_label_at(jan), "UTC+13:45")
        # Negative offsets keep their sign and pad correctly.
        self.assertEqual(tz("America/New_York").offset_label_at(jan), "UTC-05:00")
        self.assertEqual(tz("America/New_York").offset_label_at(jul), "UTC-04:00")
        self.assertEqual(tz("UTC").offset_label_at(jan), "UTC+00:00")

    def test_is_dst_at(self):
        london = tz("Europe/London")
        self.assertFalse(london.is_dst_at(datetime(2026, 1, 15, 12, tzinfo=UTC)))
        self.assertTrue(london.is_dst_at(datetime(2026, 7, 15, 12, tzinfo=UTC)))

    def test_standard_offset_both_hemispheres(self):
        # DST always ADDS, so the yearly minimum is standard time in either hemisphere.
        self.assertEqual(
            tz("America/New_York").standard_offset(2026), timedelta(hours=-5)
        )
        self.assertEqual(
            tz("Australia/Sydney").standard_offset(2026), timedelta(hours=10)
        )
        self.assertEqual(tz("Asia/Kolkata").standard_offset(2026), timedelta(hours=5, minutes=30))


class ToUtcTests(SimpleTestCase):
    def test_ordinary_conversion(self):
        got, flag = tz("America/New_York").to_utc(date(2026, 6, 1), time(9, 0))
        self.assertEqual(got, datetime(2026, 6, 1, 13, 0, tzinfo=UTC))
        self.assertEqual(flag, "")

    def test_nonexistent_snaps_to_transition_instant(self):
        # 02:30 does not exist on 2026-03-08 in New York; the clock jumps 02:00 -> 03:00.
        got, flag = tz("America/New_York").to_utc(date(2026, 3, 8), time(2, 30))
        self.assertEqual(flag, "nonexistent")
        self.assertEqual(got, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))
        # 07:00Z is exactly 03:00 local — the first instant that exists.
        self.assertEqual(got.astimezone(ZoneInfo("America/New_York")).hour, 3)

    def test_gap_interval_is_not_zero_length(self):
        """Regression: snapping by the gap WIDTH instead of to the transition
        instant makes 02:30->03:30 collapse to zero length, which then trips the
        end>start constraint on any derived interval row."""
        ny = tz("America/New_York")
        start, _ = ny.to_utc(date(2026, 3, 8), time(2, 30), prefer_fold=0)
        end, _ = ny.to_utc(date(2026, 3, 8), time(3, 30), prefer_fold=1)
        self.assertGreater(end, start)
        self.assertEqual(end - start, timedelta(minutes=30))

    def test_ambiguous_fold_selects_occurrence(self):
        # 01:30 happens twice on 2026-11-01 in New York.
        ny = tz("America/New_York")
        first, flag_a = ny.to_utc(date(2026, 11, 1), time(1, 30), prefer_fold=0)
        second, flag_b = ny.to_utc(date(2026, 11, 1), time(1, 30), prefer_fold=1)
        self.assertEqual(flag_a, "ambiguous")
        self.assertEqual(flag_b, "ambiguous")
        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(second, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
        self.assertEqual(second - first, timedelta(hours=1))

    def test_interval_across_transitions_has_real_elapsed_time(self):
        """A 22:00->06:00 overnight block loses an hour in spring and gains one in
        autumn. This is the property the whole conversion policy exists to get right."""
        ny = tz("America/New_York")

        def block(anchor):
            start, _ = ny.to_utc(anchor, time(22, 0), prefer_fold=0)
            end, _ = ny.to_utc(anchor + timedelta(days=1), time(6, 0), prefer_fold=1)
            return end - start

        self.assertEqual(block(date(2026, 6, 1)), timedelta(hours=8))   # ordinary
        self.assertEqual(block(date(2026, 3, 7)), timedelta(hours=7))   # spring forward
        self.assertEqual(block(date(2026, 10, 31)), timedelta(hours=9))  # fall back

    def test_same_wall_clock_differs_by_season(self):
        london = tz("Europe/London")
        winter, _ = london.to_utc(date(2026, 1, 15), time(9, 0))
        summer, _ = london.to_utc(date(2026, 7, 15), time(9, 0))
        self.assertEqual(winter.hour, 9)
        self.assertEqual(summer.hour, 8)

    def test_offset_column_is_not_used_for_arithmetic(self):
        """The stored display string must have no influence on conversion."""
        ny = tz("America/New_York")
        ny.offset = "UTC+99:00"
        got, _ = ny.to_utc(date(2026, 6, 1), time(9, 0))
        self.assertEqual(got, datetime(2026, 6, 1, 13, 0, tzinfo=UTC))


class ValidatorTests(SimpleTestCase):
    def test_rejects_unknown_zone(self):
        with self.assertRaises(ValidationError):
            validate_iana_timezone("Mars/Olympus_Mons")

    def test_rejects_empty_and_malformed(self):
        for bad in ("", "   ", "GMT+5", "Not/A/Zone"):
            with self.assertRaises(ValidationError, msg=f"accepted {bad!r}"):
                validate_iana_timezone(bad)

    def test_accepts_real_zones(self):
        for good in ("UTC", "Europe/London", "America/New_York", "Asia/Kolkata"):
            validate_iana_timezone(good)  # must not raise


class FullCleanTests(TestCase):
    """`full_clean()` runs the unique check too, so these need database access."""

    def test_full_clean_rejects_bad_timezone(self):
        obj = Timezone(timezone="Mars/Olympus_Mons", offset="UTC+00:00", region="Mars")
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("timezone", ctx.exception.error_dict)

    def test_full_clean_accepts_good_timezone(self):
        Timezone(
            timezone="Europe/London", offset="UTC+00:00", region="London"
        ).full_clean()


class SeedDataTests(SimpleTestCase):
    """The seed file is the real source of Timezone rows, so it is what must be valid."""

    def _seed_rows(self):
        path = Path(settings.BASE_DIR) / "json_files" / "timezone.json"
        return json.loads(path.read_text(encoding="utf-8"))["data"]

    def test_every_seeded_zone_resolves(self):
        """Catches a bad seed entry AND a missing `tzdata` package in CI — on a slim
        container without system zoneinfo, ZoneInfo() raises and availability dies."""
        failures = []
        for row in self._seed_rows():
            try:
                validate_iana_timezone(row["timezone"])
            except ValidationError as exc:
                failures.append((row["timezone"], str(exc)))
        self.assertEqual(failures, [], f"unresolvable seeded zones: {failures}")

    def test_seed_is_not_empty(self):
        self.assertGreater(len(self._seed_rows()), 0)


class AuditCommandTests(TestCase):
    def test_reports_and_fixes_drift(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        Timezone.objects.create(
            timezone="America/New_York", offset="UTC+99:00", region="New York"
        )
        Timezone.objects.create(
            timezone="Asia/Kolkata", offset="UTC+05:30", region="Kolkata"
        )

        # Without --fix the command must fail loudly so cron/CI can gate on it.
        with self.assertRaises(CommandError):
            call_command("audit_timezone_offsets", "--year", 2026, stdout=StringIO())

        call_command("audit_timezone_offsets", "--fix", "--year", 2026, stdout=StringIO())
        self.assertEqual(
            Timezone.objects.get(timezone="America/New_York").offset, "UTC-05:00"
        )
        # A correct row is left untouched.
        self.assertEqual(
            Timezone.objects.get(timezone="Asia/Kolkata").offset, "UTC+05:30"
        )

        # Second run is clean.
        call_command("audit_timezone_offsets", "--year", 2026, stdout=StringIO())
