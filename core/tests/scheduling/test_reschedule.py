from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.event_template import EventTemplate
from core.services import bookings as booking_service, reschedule
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at
from core.tests.scheduling.test_calendar import FakeProvider, make_integration

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class RescheduleEngineTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)  # ROUND_ROBIN

    def _book(self, template, host, start=None):
        return booking_service.create_booking(
            tenant=self.tenant,
            event_template=template,
            start_time=start or at(10),
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )

    def test_leave_reassigns_round_robin(self):
        host1 = f.make_host(self.tenant, self.template)
        host2 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["reassigned"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.primary_host_id, host2.id)
        self.assertEqual(booking.status, Booking.Status.SCHEDULED)
        self.assertTrue(booking.events.filter(event_type="REASSIGNED").exists())

    def test_leave_flags_when_no_other_host(self):
        host1 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["flagged"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)
        self.assertIsNotNone(booking.reschedule_token)
        self.assertIsNotNone(booking.reschedule_token_expires_at)
        self.assertTrue(booking.events.filter(event_type="RESCHEDULE_REQUESTED").exists())

    def test_direct_strategy_never_auto_reassigns(self):
        template = f.make_template(
            self.tenant, assignment_strategy=EventTemplate.AssignmentStrategy.DIRECT
        )
        host1 = f.make_host(self.tenant, template)
        f.make_host(self.tenant, template)  # a free second host exists…
        booking = self._book(template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        # …but DIRECT bookings must not be silently moved.
        self.assertEqual(result["flagged"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)

    def test_reassignment_skips_host_also_on_leave(self):
        host1 = f.make_host(self.tenant, self.template)
        host2 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        f.make_exception(self.tenant, host2, at(9), at(12))  # host2 unavailable too
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["flagged"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)

    def test_reassignment_never_crosses_tenant(self):
        host1 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        # Another tenant has a completely free host at the same time.
        other_tenant = f.make_tenant()
        other_template = f.make_template(other_tenant)
        f.make_host(other_tenant, other_template)
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["reassigned"], 0)
        self.assertEqual(result["flagged"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)

    def test_idempotent_flag(self):
        host1 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        first = reschedule.handle_leave(self.tenant, leave.id, now=NOW)
        second = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(first["flagged"], 1)
        # Already-flagged bookings are excluded from reprocessing entirely.
        self.assertEqual(second, {"reassigned": 0, "flagged": 0, "skipped": 0})
        self.assertEqual(
            BookingEvent.objects.filter(
                booking=booking, event_type="RESCHEDULE_REQUESTED"
            ).count(),
            1,
        )

    def test_idempotent_reassign(self):
        host1 = f.make_host(self.tenant, self.template)
        f.make_host(self.tenant, self.template)
        self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        first = reschedule.handle_leave(self.tenant, leave.id, now=NOW)
        second = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(first["reassigned"], 1)
        # Booking is no longer on the leaving host, so it isn't reprocessed.
        self.assertEqual(second, {"reassigned": 0, "flagged": 0, "skipped": 0})

    def test_signal_triggers_reschedule_end_to_end(self):
        host1 = f.make_host(self.tenant, self.template)
        host2 = f.make_host(self.tenant, self.template)
        start = (timezone.now() + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=host1.id,
        )
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            f.make_exception(
                self.tenant, host1, start - timedelta(hours=1), start + timedelta(hours=2)
            )
        booking.refresh_from_db()
        self.assertEqual(booking.primary_host_id, host2.id)  # reassigned via signal->task

    @patch("core.services.calendars.get_provider_for")
    def test_reassignment_resyncs_calendars(self, mock_provider):
        fake = FakeProvider(push_id="new-evt")
        mock_provider.return_value = fake
        host1 = f.make_host(self.tenant, self.template)
        host2 = f.make_host(self.tenant, self.template)
        make_integration(self.tenant, host1)
        make_integration(self.tenant, host2)

        booking = self._book(self.template, host1, at(10))
        Booking.objects.filter(pk=booking.pk).update(
            external_event_id="old-evt", external_provider="GOOGLE"
        )
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        with self.captureOnCommitCallbacks(execute=True):
            result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["reassigned"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.primary_host_id, host2.id)
        self.assertIn("old-evt", fake.deleted)  # old host's event removed
        self.assertEqual(booking.external_event_id, "new-evt")  # re-pushed to new host
