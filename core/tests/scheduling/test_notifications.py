from unittest import skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase

from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service, notifications
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class NotificationTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)

    def test_confirmation_sent_once_with_ics(self):
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        filenames = [att[0] for att in message.attachments]
        self.assertIn("invite.ics", filenames)
        ics_blob = next(att[1] for att in message.attachments if att[0] == "invite.ics")
        self.assertIn("BEGIN:VCALENDAR", ics_blob)

        # Idempotent: a second dispatch must not send another email.
        notifications.send_confirmation(booking.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            NotificationLog.objects.filter(
                booking=booking,
                notification_type=NotificationLog.NotificationType.CONFIRMATION,
            ).count(),
            1,
        )

    def test_cancellation_email_sent(self):
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            booking_service.cancel_booking(
                booking=booking, enforce_lead=False, now=NOW
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("cancel", mail.outbox[0].subject.lower())
