from datetime import date, time
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.user_event_skill import UserEventSkill
from core.services import availability, bookings as booking_service
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, UTC, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class AvailabilityEngineTests(TestCase):
    def test_dst_aware_slots(self):
        """9:00 local converts to 14:00 UTC in EST (winter) and 13:00 UTC in EDT (summer)."""
        tenant = f.make_tenant()
        tz = f.make_timezone("America/New_York", "UTC-05:00", "New York")
        template = f.make_template(
            tenant, duration_minutes=60, slot_granularity_minutes=60, max_horizon_days=365
        )
        host = f.make_user(tenant)
        UserEventSkill.objects.create(tenant=tenant, user=host, event_template=template)
        profile = AvailabilityProfile.objects.create(
            tenant=tenant, user=host, timezone=tz
        )
        winter = date(2027, 1, 11)
        summer = date(2027, 7, 12)
        for weekday in {winter.weekday(), summer.weekday()}:
            AvailabilityRule.objects.create(
                tenant=tenant,
                profile=profile,
                weekday=weekday,
                start_time=time(9, 0),
                end_time=time(17, 0),
            )
        # Explicit range: this test straddles a DST transition seven months wide,
        # far outside the factory's default anchors.
        f.materialize(profile, date(2027, 1, 1), date(2027, 7, 31))
        now = at(0, 0, day=1, month=1)  # 2027-01-01

        # The host's LOCAL day, which is what a New York user picking a date means.
        winter_slots = availability.get_available_slots(
            tenant, template, *f.local_day(winter, tz), target_host_id=host.id, now=now
        )
        summer_slots = availability.get_available_slots(
            tenant, template, *f.local_day(summer, tz), target_host_id=host.id, now=now
        )
        self.assertTrue(winter_slots and summer_slots)
        self.assertEqual(winter_slots[0].start.astimezone(UTC).hour, 14)  # EST -5
        self.assertEqual(summer_slots[0].start.astimezone(UTC).hour, 13)  # EDT -4

    def test_booked_slot_excluded_from_availability(self):
        tenant = f.make_tenant()
        template = f.make_template(tenant, duration_minutes=30, slot_granularity_minutes=30)
        host = f.make_host(tenant, template)
        start = at(10)
        booking_service.create_booking(
            tenant=tenant,
            event_template=template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )
        slots = availability.get_available_slots(
            tenant, template, *f.utc_day(date(2027, 3, 16)),
            target_host_id=host.id, now=NOW,
        )
        starts = {s.start for s in slots}
        self.assertNotIn(start, starts)
        self.assertTrue(slots)  # other slots still available

    def test_min_notice_filters_early_slots(self):
        tenant = f.make_tenant()
        template = f.make_template(
            tenant, duration_minutes=30, slot_granularity_minutes=30, min_notice_minutes=1440
        )
        host = f.make_host(tenant, template)
        # now = 2027-03-16 08:00; min notice 24h -> nothing bookable on 03-16
        now = at(8, 0, day=16)
        slots = availability.get_available_slots(
            tenant, template, *f.utc_day(date(2027, 3, 16)),
            target_host_id=host.id, now=now,
        )
        self.assertEqual(slots, [])

    def test_host_on_leave_excluded_from_slots(self):
        tenant = f.make_tenant()
        template = f.make_template(
            tenant, duration_minutes=60, slot_granularity_minutes=60
        )
        host = f.make_host(tenant, template)
        f.make_exception(tenant, host, at(9), at(12))  # leave 09:00-12:00

        slots = availability.get_available_slots(
            tenant, template, *f.utc_day(date(2027, 3, 16)),
            target_host_id=host.id, now=NOW,
        )
        starts = {s.start for s in slots}
        self.assertNotIn(at(9), starts)
        self.assertNotIn(at(10), starts)
        self.assertNotIn(at(11), starts)
        self.assertIn(at(12), starts)  # slot starting when leave ends is free

    def test_tenant_isolation_in_slots(self):
        tenant_a = f.make_tenant()
        tenant_b = f.make_tenant()
        template_a = f.make_template(tenant_a)
        host_a = f.make_host(tenant_a, template_a)
        template_b = f.make_template(tenant_b)
        host_b = f.make_host(tenant_b, template_b)

        slots = availability.get_available_slots(
            tenant_a, template_a, *f.utc_day(date(2027, 3, 16)), now=NOW
        )
        host_ids = {h for s in slots for h in s.eligible_host_ids}
        self.assertIn(host_a.id, host_ids)
        self.assertNotIn(host_b.id, host_ids)
