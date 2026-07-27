from datetime import timedelta
from unittest import skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service, reminders
from core import tasks
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class ReminderTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)

    def _book(self, start=None):
        return booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start or at(10),
            participants=ATTENDEES,
            target_host_id=self.host.id,
            now=NOW,
        )

    def test_one_hour_reminder_sent_once(self):
        self._book(at(10))  # 2027-03-16 10:00
        reminder_now = at(9, 15)  # 45 minutes before start

        sent = reminders.send_due_reminders(now=reminder_now)
        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            NotificationLog.objects.filter(dedup_key__endswith="REMINDER:60").exists()
        )

        # Re-run at the same moment: no duplicate.
        self.assertEqual(reminders.send_due_reminders(now=reminder_now), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_24h_reminder_offset(self):
        self._book(at(10))  # 2027-03-16 10:00
        reminder_now = at(10, 0, day=15)  # exactly 24h before

        sent = reminders.send_due_reminders(now=reminder_now)
        self.assertEqual(sent, 1)
        self.assertTrue(
            NotificationLog.objects.filter(dedup_key__endswith="REMINDER:1440").exists()
        )

    def test_no_reminder_for_canceled(self):
        booking = self._book(at(10))
        booking_service.cancel_booking(booking=booking, enforce_lead=False, now=NOW)
        self.assertEqual(reminders.send_due_reminders(now=at(9, 15)), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_cleanup_expired_holds_task(self):
        start = (timezone.now() + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        hold = booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=self.host.id,
        )
        from core.models.booking import Booking

        Booking.objects.filter(pk=hold.pk).update(
            hold_expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(tasks.cleanup_expired_holds(), 1)
        hold.refresh_from_db()
        self.assertEqual(hold.status, Booking.Status.CANCELED)
