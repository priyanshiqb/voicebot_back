"""
Two failure modes that give the customer the wrong impression of what happened:

  P0-5  a post-commit side effect raising turns a durably-created booking into a
        500, so the customer believes it failed, retries, and 409s against their
        own row.
  P1-2  NotificationLog was marked SENT at enqueue time, so an email that failed
        every retry still reported success and could never be re-sent.
"""

from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models.booking import Booking
from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service, deferred, notifications
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


class DeferredSafeTests(TestCase):
    """`deferred.safe` is the guard itself; test it directly and in place."""

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_swallows_and_logs(self):
        def boom():
            raise RuntimeError("broker unreachable")

        with self.assertLogs("core.services.deferred", level="ERROR") as logs:
            self.assertIsNone(deferred.safe(boom)())
        self.assertTrue(any("boom" in line for line in logs.output))

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_passes_through_arguments_and_result(self):
        self.assertEqual(deferred.safe(lambda a, b: a + b, 2, 3)(), 5)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_reraises_under_eager_so_tests_do_not_hide_bugs(self):
        def boom():
            raise RuntimeError("real bug")

        with self.assertLogs("core.services.deferred", level="ERROR"):
            with self.assertRaises(RuntimeError):
                deferred.safe(boom)()


@REQUIRES_PG
class BookingSurvivesBrokerFailureTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch("core.tasks.push_booking_to_calendar.delay", side_effect=OSError("broker down"))
    @patch("core.tasks.send_scheduling_email_task.delay", side_effect=OSError("broker down"))
    def test_booking_survives_unreachable_broker(self, _mail, _cal):
        """The booking is committed before the callbacks run. If enqueueing raises,
        the caller must still get the booking — not a 500 for work that succeeded."""
        with self.assertLogs("core.services.deferred", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                booking = booking_service.create_booking(
                    tenant=self.tenant,
                    event_template=self.template,
                    start_time=self.start,
                    participants=[{"name": "C", "email": "c@example.com"}],
                    target_host_id=self.host.id,
                    now=self.now,
                )

        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.SCHEDULED)
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch("core.tasks.delete_booking_calendar_event.delay", side_effect=OSError("broker down"))
    @patch("core.tasks.send_scheduling_email_task.delay", side_effect=OSError("broker down"))
    def test_cancel_survives_unreachable_broker(self, _mail, _cal):
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=self.start,
                participants=[{"name": "C", "email": "c@example.com"}],
                target_host_id=self.host.id,
                now=self.now,
            )
        with self.assertLogs("core.services.deferred", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                booking_service.cancel_booking(
                    booking=booking, enforce_lead=False, now=self.now
                )
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELED)


@REQUIRES_PG
class NotificationLogTruthfulnessTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def _book(self):
        with self.captureOnCommitCallbacks(execute=True):
            return booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=self.start,
                participants=[{"name": "C", "email": "c@example.com"}],
                target_host_id=self.host.id,
                now=self.now,
            )

    def test_successful_send_marks_sent_with_timestamp(self):
        booking = self._book()
        log = NotificationLog.objects.get(
            booking=booking,
            notification_type=NotificationLog.NotificationType.CONFIRMATION,
        )
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(log.error, "")
        self.assertEqual(len(mail.outbox), 1)

    @patch("core.tasks.EmailMultiAlternatives.send", side_effect=OSError("smtp down"))
    def test_permanent_failure_marks_failed_with_error(self, _send):
        """Regression: this used to read SENT forever. FAILED and `error` were
        declared on the model and written by no code path at all.

        Invoked with `retries` already at the ceiling rather than through a
        booking: under CELERY_TASK_ALWAYS_EAGER, `self.retry()` raises `Retry`
        immediately and `request.retries` never advances, so retry exhaustion
        cannot be reached end-to-end in tests. This exercises the same branch the
        4th real attempt takes.
        """
        from core.tasks import send_scheduling_email_task

        log = NotificationLog.objects.create(
            tenant=self.tenant,
            channel=NotificationLog.Channel.EMAIL,
            notification_type=NotificationLog.NotificationType.CONFIRMATION,
            recipient="c@example.com",
            dedup_key="terminal-failure-test",
            status=NotificationLog.Status.PENDING,
        )
        send_scheduling_email_task.apply(
            kwargs={
                "subject": "s",
                "body": "b",
                "recipient_list": ["c@example.com"],
                "notification_log_id": str(log.id),
                # Required now that booking mail resolves the tenant's own sender.
                # Without it the task takes the no-configuration branch and never
                # reaches the send this test is about.
                "tenant_id": str(self.tenant.id),
            },
            retries=send_scheduling_email_task.max_retries,
            throw=False,
        )

        log.refresh_from_db()
        self.assertEqual(log.status, NotificationLog.Status.FAILED)
        self.assertIn("smtp down", log.error)
        self.assertIsNone(log.sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_notification_can_be_resent(self):
        """A FAILED row must be re-enqueueable — previously impossible, because the
        short-circuit only ever saw SENT."""
        booking = self._book()
        mail.outbox.clear()
        log = NotificationLog.objects.get(booking=booking)
        NotificationLog.objects.filter(pk=log.pk).update(
            status=NotificationLog.Status.FAILED,
            sent_at=None,
            error="smtp down",
        )

        self.assertTrue(notifications.send_confirmation(booking.id))
        log.refresh_from_db()
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertEqual(log.error, "")
        self.assertEqual(len(mail.outbox), 1)

    def test_sent_notification_is_not_resent(self):
        booking = self._book()
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(notifications.send_confirmation(booking.id))
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch("core.tasks.send_scheduling_email_task.delay")
    def test_in_flight_pending_is_not_re_enqueued(self, mock_delay):
        """With SENT now written by the task, a still-queued row must not be
        re-enqueued by the next caller — that would double-send reminders, which
        the beat scans every 5 minutes."""
        booking = self._book()
        self.assertEqual(mock_delay.call_count, 1)
        log = NotificationLog.objects.get(booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.PENDING)

        self.assertFalse(notifications.send_confirmation(booking.id))
        self.assertEqual(mock_delay.call_count, 1)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch("core.tasks.send_scheduling_email_task.delay")
    def test_stale_pending_is_re_enqueued(self, mock_delay):
        """Past the in-flight window a stuck PENDING becomes re-sendable — this is
        how a message the broker lost recovers."""
        booking = self._book()
        log = NotificationLog.objects.get(booking=booking)
        NotificationLog.objects.filter(pk=log.pk).update(
            updated_at=timezone.now() - notifications.PENDING_IN_FLIGHT_GRACE
            - timedelta(minutes=1)
        )
        self.assertTrue(notifications.send_confirmation(booking.id))
        self.assertEqual(mock_delay.call_count, 2)
