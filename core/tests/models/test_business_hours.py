"""
Business hours resolution.

Every case from the design walkthrough is here, because the point of this schema
over the usual one-start-one-end-per-day table is precisely that it can express
them. Two break naive implementations:

* **Split shifts** — needs several rows per weekday, which a
  `unique_together(schedule, weekday)` forbids outright.
* **Overnight blocks** — 02:00 on Sunday is covered by Saturday's 22:00-02:00, so
  resolution has to look at the previous day too.

Date-specific overrides (holidays, one-off closures) are not modelled yet, so
nothing here covers them.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.test import TestCase

from core.models.business_hours import BusinessHours, BusinessHoursInterval
from onboard.models.timezone import Timezone
from tenant.models.Tenant import Tenant

KOLKATA = ZoneInfo("Asia/Kolkata")


class BusinessHoursTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-hours")
        self.timezone, _ = Timezone.objects.get_or_create(
            timezone="Asia/Kolkata",
            defaults={"offset": "UTC+05:30", "region": "Asia"},
        )
        self.hours = BusinessHours.objects.create(
            tenant=self.tenant, timezone=self.timezone
        )

    def interval(self, weekday, opens, closes):
        return BusinessHoursInterval.objects.create(
            tenant=self.tenant,
            business_hours=self.hours,
            weekday=weekday,
            opens_at=time.fromisoformat(opens),
            closes_at=time.fromisoformat(closes),
        )

    def at(self, when):
        """A local wall-clock moment in the tenant's timezone."""
        return datetime.fromisoformat(when).replace(tzinfo=KOLKATA)


class WeeklyPatternTests(BusinessHoursTestCase):
    def test_a_simple_day(self):
        self.interval(0, "09:00", "17:00")  # Monday

        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T09:00")))
        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T16:59")))
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-03T08:59")))
        # Closing time is exclusive — 17:00 is shut, not the last open minute.
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-03T17:00")))

    def test_a_day_with_no_rows_is_closed(self):
        """Closed is the absence of intervals, not a status flag — so there is no
        way to be closed-with-hours."""
        self.interval(0, "09:00", "17:00")
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-09T12:00")))  # Sunday

    def test_a_split_shift(self):
        """The case a `unique_together(schedule, weekday)` would forbid forever."""
        self.interval(0, "09:00", "13:00")
        self.interval(0, "14:00", "18:00")

        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T10:00")))
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-03T13:30")))
        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T15:00")))

    def test_an_overnight_block_covers_the_small_hours_of_the_next_day(self):
        """Saturday 22:00-02:00 has to answer for 01:00 on Sunday, which means
        resolution cannot look only at the current date."""
        self.interval(5, "22:00", "02:00")  # Saturday

        self.assertTrue(self.hours.is_open_at(self.at("2026-08-08T23:30")))
        self.assertTrue(self.hours.is_open_at(self.at("2026-08-09T01:00")))
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-09T02:00")))
        self.assertFalse(self.hours.is_open_at(self.at("2026-08-08T21:00")))

    def test_midnight_to_midnight_is_a_full_day(self):
        self.interval(0, "00:00", "00:00")

        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T00:00")))
        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T23:59")))

    def test_times_are_read_in_the_tenants_timezone_not_utc(self):
        """09:00-17:00 in Kolkata is 03:30-11:30 UTC. Reading the wall clock as UTC
        would report the business open through the night."""
        self.interval(0, "09:00", "17:00")

        utc_morning = datetime(2026, 8, 3, 4, 0, tzinfo=ZoneInfo("UTC"))
        self.assertTrue(self.hours.is_open_at(utc_morning))

        utc_evening = datetime(2026, 8, 3, 15, 0, tzinfo=ZoneInfo("UTC"))
        self.assertFalse(self.hours.is_open_at(utc_evening))


class MasterSwitchTests(BusinessHoursTestCase):
    def test_closing_the_business_overrides_everything(self):
        self.interval(0, "00:00", "00:00")
        self.hours.is_open = False
        self.hours.save(update_fields=["is_open"])

        self.assertFalse(self.hours.is_open_at(self.at("2026-08-03T12:00")))

    def test_reopening_restores_the_schedule_untouched(self):
        """The reason it is a flag and not a delete."""
        self.interval(0, "09:00", "17:00")
        self.hours.is_open = False
        self.hours.save(update_fields=["is_open"])
        self.hours.is_open = True
        self.hours.save(update_fields=["is_open"])

        self.assertTrue(self.hours.is_open_at(self.at("2026-08-03T12:00")))


class SchemaTests(BusinessHoursTestCase):
    def test_one_schedule_per_tenant(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BusinessHours.objects.create(
                    tenant=self.tenant, timezone=self.timezone
                )

    def test_wraps_midnight_reports_the_overnight_case(self):
        self.assertTrue(self.interval(5, "22:00", "02:00").wraps_midnight)
        self.assertFalse(self.interval(0, "09:00", "17:00").wraps_midnight)
        # 00:00 -> 00:00 is 24 hours, which also wraps.
        self.assertTrue(self.interval(1, "00:00", "00:00").wraps_midnight)
