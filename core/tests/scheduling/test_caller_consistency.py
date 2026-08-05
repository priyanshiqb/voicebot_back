"""
Three places where two callers answered the same question differently.

  P1-1  an unreachable external calendar returned [] — indistinguishable from
        "the host is free" — so an outage silently advertised, and booked, slots
        the host was actually busy for.
  P1-3  `is_active` was honoured by the AI bridge but not by the REST API, so a
        deactivated template stayed bookable over HTTP.
  P1-4  leave and external-busy were tested against raw times on the commit path
        but buffer-inclusive times on the listing path, so the two disagreed
        about the same slot.
"""

from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.calendar_integration import CalendarIntegration
from core.serializers.availability import AvailableSlotsQuerySerializer
from core.serializers.booking import BookingCreateSerializer
from core.services import availability, bookings as booking_service
from core.services.exceptions import ExternalCalendarUnavailable
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


def make_integration(tenant, user):
    integration = CalendarIntegration(
        tenant=tenant,
        user=user,
        provider=CalendarIntegration.Provider.GOOGLE,
        account_email="host@gmail.com",
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    )
    integration.set_tokens(access_token="access", refresh_token="refresh")
    integration.save()
    return integration


class _Base(TestCase):
    def setUp(self):
        cache.clear()  # free/busy results are cached per window
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant, buffer_before_minutes=30)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )


@REQUIRES_PG
class InactiveTemplateTests(_Base):
    def test_rest_booking_rejects_inactive_template(self):
        """The AI bridge already filtered is_active; REST did not. An admin who
        deactivates a template reasonably believes it is off everywhere."""
        self.template.is_active = False
        self.template.save(update_fields=["is_active"])

        serializer = BookingCreateSerializer(
            data={
                "event_template": str(self.template.id),
                "start_time": self.start.isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "C", "email": "c@example.com"}],
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("event_template", serializer.errors)

    def test_slots_query_rejects_inactive_template(self):
        self.template.is_active = False
        self.template.save(update_fields=["is_active"])

        serializer = AvailableSlotsQuerySerializer(
            data={
                "event_template": str(self.template.id),
                "date_from": self.start.date().isoformat(),
                "date_to": self.start.date().isoformat(),
                "target_host": str(self.host.id),
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("event_template", serializer.errors)

    def test_active_template_still_accepted(self):
        serializer = BookingCreateSerializer(
            data={
                "event_template": str(self.template.id),
                "start_time": self.start.isoformat(),
                "target_host": str(self.host.id),
                "participants": [{"name": "C", "email": "c@example.com"}],
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


@REQUIRES_PG
class BufferConsistencyTests(_Base):
    def test_leave_overlapping_only_the_buffer_blocks_both_paths(self):
        """The template has a 30-minute pre-buffer. Leave covering 09:45-09:55 sits
        entirely inside that buffer and outside the meeting itself.

        Listing already treated it as busy; the commit path did not, so the same
        slot was hidden and bookable at once.
        """
        f.make_exception(
            self.tenant,
            self.host,
            self.start - timedelta(minutes=15),
            self.start - timedelta(minutes=5),
        )

        day = self.start.date()
        slots = availability.get_available_slots(
            self.tenant, self.template, *f.utc_day(day),
            target_host_id=self.host.id, now=self.now,
        )
        self.assertNotIn(self.start, {s.start for s in slots})

        free = availability.eligible_free_hosts(
            self.tenant,
            self.template,
            self.start,
            self.start + timedelta(minutes=self.template.duration_minutes),
            target_host_id=self.host.id,
        )
        self.assertEqual(free, [], "commit path disagreed with the listing path")


@REQUIRES_PG
class ExternalCalendarDegradationTests(_Base):
    def setUp(self):
        super().setUp()
        self.integration = make_integration(self.tenant, self.host)

    def _boom(self, *_a, **_k):
        raise OSError("google unreachable")

    def test_healthy_empty_calendar_is_not_degraded(self):
        """An empty result from a working lookup means free — it must not be
        confused with an unreachable provider."""
        with patch("core.services.calendars.get_provider_for") as provider:
            provider.return_value.get_busy.return_value = []
            intervals, degraded = availability._external_busy(
                self.tenant, self.host.id, self.start, self.start + timedelta(hours=1)
            )
        self.assertEqual(intervals, [])
        self.assertFalse(degraded)

    def test_no_integration_is_not_degraded(self):
        self.integration.delete()
        intervals, degraded = availability._external_busy(
            self.tenant, self.host.id, self.start, self.start + timedelta(hours=1)
        )
        self.assertEqual(intervals, [])
        self.assertFalse(degraded)

    @patch("core.services.calendars.get_provider_for")
    def test_provider_failure_is_degraded_and_recorded(self, provider):
        provider.side_effect = self._boom
        with self.assertLogs("core.services.availability", level="ERROR"):
            intervals, degraded = availability._external_busy(
                self.tenant, self.host.id, self.start, self.start + timedelta(hours=1)
            )
        self.assertEqual(intervals, [])
        self.assertTrue(degraded)

        self.integration.refresh_from_db()
        self.assertIn("google unreachable", self.integration.last_error)

    @patch("core.services.calendars.get_provider_for")
    def test_browsing_still_returns_slots_but_flags_them(self, provider):
        """A provider outage must not empty the calendar — that would take the
        product down. It must be visible, though."""
        provider.side_effect = self._boom
        day = self.start.date()
        with self.assertLogs("core.services.availability", level="ERROR"):
            slots = availability.get_available_slots(
                self.tenant, self.template, *f.utc_day(day),
                target_host_id=self.host.id, now=self.now,
            )
        self.assertTrue(slots)
        self.assertTrue(all(s.external_check_failed for s in slots))

    @patch("core.services.calendars.get_provider_for")
    def test_committing_refuses_when_calendar_unverified(self, provider):
        provider.side_effect = self._boom
        with self.assertLogs("core.services.availability", level="ERROR"):
            with self.assertRaises(ExternalCalendarUnavailable):
                booking_service.create_booking(
                    tenant=self.tenant,
                    event_template=self.template,
                    start_time=self.start,
                    participants=[{"name": "C", "email": "c@example.com"}],
                    target_host_id=self.host.id,
                    now=self.now,
                )

    @patch("core.services.calendars.get_provider_for")
    def test_healthy_provider_still_books(self, provider):
        provider.return_value.get_busy.return_value = []
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=[{"name": "C", "email": "c@example.com"}],
            target_host_id=self.host.id,
            now=self.now,
        )
        self.assertIsNotNone(booking.pk)

    @patch("core.services.calendars.get_provider_for")
    def test_slots_are_unflagged_when_provider_is_healthy(self, provider):
        provider.return_value.get_busy.return_value = []
        slots = availability.get_available_slots(
            self.tenant, self.template, *f.utc_day(self.start.date()),
            target_host_id=self.host.id, now=self.now,
        )
        self.assertTrue(slots)
        self.assertFalse(any(s.external_check_failed for s in slots))
