"""
Notification log attribution and retention.

The retention task is the one piece of this that can cause harm if it is wrong, in
a way that is not obvious: **a NotificationLog row is the idempotency mechanism,
not a record of it.** Deleting a row frees its `dedup_key`, which re-arms the
notification it represented. A purge that ignores that would re-send confirmations
for bookings still in the future.
"""

from datetime import timedelta
from unittest import skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service
from core.tasks import purge_notification_logs
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at
from tenant.models.TenantSettings import TenantSettings


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class NotificationAttributionTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        mail.outbox.clear()

    def test_a_sent_notification_records_which_configuration_carried_it(self):
        """Without this, a tenant holding several configurations cannot tell which
        one a message went through — the question worth asking when deliverability
        changes after a provider switch."""
        config = TenantSettings.booking_sender(self.tenant.id)

        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )

        log = NotificationLog.objects.get(booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertEqual(log.email_configuration_id, config.pk)

    def test_a_message_never_sent_has_no_configuration_recorded(self):
        TenantSettings.objects.filter(tenant=self.tenant).update(
            booking_email_configuration=None
        )
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )

        log = NotificationLog.objects.get(booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.FAILED)
        self.assertIsNone(log.email_configuration_id)


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class NotificationRetentionTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.now = timezone.now()

    def _log(self, *, age_days, status=NotificationLog.Status.SENT, booking=None, key=None):
        log = NotificationLog.objects.create(
            tenant=self.tenant,
            channel=NotificationLog.Channel.EMAIL,
            notification_type=NotificationLog.NotificationType.CONFIRMATION,
            recipient="customer@example.com",
            dedup_key=key or f"key-{age_days}-{status}-{id(self)}-{NotificationLog.objects.count()}",
            status=status,
            booking=booking,
        )
        # created_at is auto_now_add, so it has to be rewritten to age the row.
        NotificationLog.objects.filter(pk=log.pk).update(
            created_at=self.now - timedelta(days=age_days)
        )
        return log

    @override_settings(
        NOTIFICATION_LOG_RETENTION_DAYS=90, NOTIFICATION_LOG_FAILED_RETENTION_DAYS=180
    )
    def test_old_sent_rows_are_purged(self):
        old = self._log(age_days=120)
        recent = self._log(age_days=10)

        purge_notification_logs()

        self.assertFalse(NotificationLog.all_objects.filter(pk=old.pk).exists())
        self.assertTrue(NotificationLog.all_objects.filter(pk=recent.pk).exists())

    @override_settings(
        NOTIFICATION_LOG_RETENTION_DAYS=90, NOTIFICATION_LOG_FAILED_RETENTION_DAYS=180
    )
    def test_failures_are_kept_longer(self):
        """A FAILED row is the only trace of a message that never arrived."""
        failed = self._log(age_days=120, status=NotificationLog.Status.FAILED)
        very_old_failure = self._log(age_days=400, status=NotificationLog.Status.FAILED)

        purge_notification_logs()

        self.assertTrue(NotificationLog.all_objects.filter(pk=failed.pk).exists())
        self.assertFalse(
            NotificationLog.all_objects.filter(pk=very_old_failure.pk).exists()
        )

    @override_settings(NOTIFICATION_LOG_RETENTION_DAYS=90)
    def test_rows_for_future_bookings_survive_regardless_of_age(self):
        """The rule that makes this safe. A confirmation written today for a booking
        eleven months out must outlive the retention window — purging it frees the
        dedup key and re-arms the notification."""
        template = f.make_template(self.tenant)
        host = f.make_host(self.tenant, template)
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=host.id,
                now=NOW,
            )
        # The fixture books into 2027; relative to real "now" that is the future.
        self.assertGreater(booking.start_time, timezone.now())

        log = NotificationLog.objects.get(booking=booking)
        NotificationLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=365)
        )

        purge_notification_logs()

        self.assertTrue(NotificationLog.all_objects.filter(pk=log.pk).exists())

    @override_settings(NOTIFICATION_LOG_RETENTION_DAYS=90)
    def test_rows_with_no_booking_are_still_purged(self):
        """The future-booking guard must not accidentally protect rows that have no
        booking at all — non-booking notifications would then accumulate forever."""
        orphan = self._log(age_days=200, booking=None)

        purge_notification_logs()

        self.assertFalse(NotificationLog.all_objects.filter(pk=orphan.pk).exists())

    @override_settings(NOTIFICATION_LOG_RETENTION_DAYS=90)
    def test_the_purge_hard_deletes(self):
        """A soft delete would leave the customer's address in the table, which
        defeats the point of a retention policy."""
        old = self._log(age_days=200)

        purge_notification_logs()

        self.assertFalse(NotificationLog.all_objects.filter(pk=old.pk).exists())

    @override_settings(
        NOTIFICATION_LOG_RETENTION_DAYS=90, NOTIFICATION_LOG_PURGE_BATCH=2
    )
    def test_batching_still_removes_everything(self):
        for _ in range(5):
            self._log(age_days=200)

        removed = purge_notification_logs()

        self.assertEqual(removed, 5)
        self.assertEqual(NotificationLog.all_objects.count(), 0)

    def test_a_purged_notification_can_be_sent_again(self):
        """The consequence of deleting a row, stated as a test so nobody is
        surprised by it: retention re-arms whatever it removes."""
        template = f.make_template(self.tenant)
        host = f.make_host(self.tenant, template)
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=host.id,
                now=NOW,
            )
        mail.outbox.clear()

        from core.services import notifications

        # Still deduped while the row exists.
        self.assertFalse(notifications.send_confirmation(booking.id))
        self.assertEqual(len(mail.outbox), 0)

        NotificationLog.all_objects.filter(booking=booking).delete()

        self.assertTrue(notifications.send_confirmation(booking.id))
        self.assertEqual(len(mail.outbox), 1)
