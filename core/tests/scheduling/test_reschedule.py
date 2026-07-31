from datetime import timedelta
from unittest import skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.services import availability, bookings as booking_service, reschedule
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class RescheduleEngineTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)  # DIRECT

    def _book(self, template, host, start=None):
        return booking_service.create_booking(
            tenant=self.tenant,
            event_template=template,
            start_time=start or at(10),
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )

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

    def test_leave_never_moves_host_even_when_another_is_free(self):
        """The booked host is the host. A free colleague is not a substitute —
        the customer is asked to pick a new time instead."""
        host1 = f.make_host(self.tenant, self.template)
        free_colleague = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["flagged"], 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)
        self.assertEqual(booking.primary_host_id, host1.id)
        self.assertNotEqual(booking.primary_host_id, free_colleague.id)
        self.assertFalse(booking.events.filter(event_type="REASSIGNED").exists())

    def test_other_tenant_host_is_never_eligible(self):
        """Tenant isolation in host selection — previously covered via the
        reassignment path, which no longer exists."""
        f.make_host(self.tenant, self.template)
        other_tenant = f.make_tenant()
        other_template = f.make_template(other_tenant)
        other_host = f.make_host(other_tenant, other_template)

        free = availability.eligible_free_hosts(
            self.tenant, self.template, at(10), at(10, 30)
        )
        self.assertNotIn(other_host.id, free)

        # Naming the other tenant's host explicitly must not smuggle them in.
        self.assertEqual(
            availability.eligible_free_hosts(
                self.tenant, self.template, at(10), at(10, 30),
                target_host_id=other_host.id,
            ),
            [],
        )

    def test_idempotent_flag(self):
        host1 = f.make_host(self.tenant, self.template)
        booking = self._book(self.template, host1, at(10))
        leave = f.make_exception(self.tenant, host1, at(9), at(12))

        first = reschedule.handle_leave(self.tenant, leave.id, now=NOW)
        second = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(first["flagged"], 1)
        # Already-flagged bookings are excluded from reprocessing entirely.
        self.assertEqual(second, {"flagged": 0, "skipped": 0})
        self.assertEqual(
            BookingEvent.objects.filter(
                booking=booking, event_type="RESCHEDULE_REQUESTED"
            ).count(),
            1,
        )

    def test_signal_triggers_reschedule_end_to_end(self):
        host1 = f.make_host(self.tenant, self.template)
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
        # Flagged via signal -> task -> engine, and the customer was emailed a link.
        self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)
        self.assertIsNotNone(booking.reschedule_token)
        self.assertTrue(mail.outbox)
