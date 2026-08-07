"""
Booking email sends through the tenant's own SMTP configuration, or not at all.

There is no platform fallback. A confirmation arriving from an address the
customer has never heard of damages the tenant's brand more than a missing one
does, and silently lending the platform's sending reputation to a tenant's
customer mail is precisely what tenant SMTP exists to prevent.

The consequence is blunt and worth stating: **a tenant who has not configured a
sender receives no booking email at all.** These tests pin both halves — that a
configured tenant sends through their own server, and that an unconfigured one
sends nothing but leaves a row saying why.
"""

import smtplib
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.test import TestCase

from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service, notifications
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.models.TenantSettings import TenantSettings


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingEmailSenderTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        mail.outbox.clear()

    def _book(self):
        with self.captureOnCommitCallbacks(execute=True):
            return booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )

    def _clear_sender(self):
        TenantSettings.objects.filter(tenant=self.tenant).update(
            booking_email_configuration=None
        )

    def test_confirmation_uses_the_tenants_own_sender(self):
        config = TenantSettings.booking_sender(self.tenant.id)
        self._book()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(config.from_email, mail.outbox[0].from_email)
        self.assertIn(config.from_name, mail.outbox[0].from_email)

    def test_nothing_is_sent_when_no_sender_is_assigned(self):
        self._clear_sender()
        booking = self._book()

        self.assertEqual(len(mail.outbox), 0)
        log = NotificationLog.objects.get(booking=booking)
        self.assertEqual(log.status, NotificationLog.Status.FAILED)
        self.assertIn("does not fall back to the platform sender", log.error)
        self.assertIsNone(log.sent_at)

    def test_nothing_is_sent_when_the_assigned_sender_is_disconnected(self):
        """The pointer is set once and read for months. What it points at can be
        taken out of service in between."""
        config = TenantSettings.booking_sender(self.tenant.id)
        TenantEmailConfiguration.objects.filter(pk=config.pk).update(is_active=False)

        self._book()
        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_is_sent_when_the_assigned_sender_is_unverified(self):
        """A credential edit resets status to UNVERIFIED long after the pointer was
        set. An untested sender must not carry live customer mail."""
        config = TenantSettings.booking_sender(self.tenant.id)
        TenantEmailConfiguration.objects.filter(pk=config.pk).update(
            status=TenantEmailConfiguration.Status.UNVERIFIED
        )

        self._book()
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unsent_notification_is_sent_once_a_sender_is_configured(self):
        """The FAILED row is the recovery path: configure a sender, and the same
        logical notification goes out rather than being lost."""
        self._clear_sender()
        booking = self._book()
        self.assertEqual(len(mail.outbox), 0)

        f.make_email_sender(self.tenant)

        self.assertTrue(notifications.send_confirmation(booking.id))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            NotificationLog.objects.get(booking=booking).status,
            NotificationLog.Status.SENT,
        )

    def test_a_permanent_smtp_failure_marks_the_configuration_failed(self):
        """Otherwise the configuration still reads VERIFIED from a test send that
        passed weeks ago, while every message silently disappears."""
        config = TenantSettings.booking_sender(self.tenant.id)
        with patch(
            "core.tasks.EmailMultiAlternatives.send",
            side_effect=smtplib.SMTPAuthenticationError(535, b"denied"),
        ):
            self._book()

        config.refresh_from_db()
        self.assertEqual(config.status, TenantEmailConfiguration.Status.FAILED)
        self.assertTrue(config.last_error)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_successful_send_stamps_last_used_at(self):
        config = TenantSettings.booking_sender(self.tenant.id)
        self.assertIsNone(config.last_used_at)

        self._book()

        config.refresh_from_db()
        self.assertIsNotNone(config.last_used_at)

    def test_platform_mail_is_unaffected(self):
        """Signup, verification and password reset must never depend on a tenant's
        SMTP — a tenant does not exist yet at signup, and one whose configuration
        breaks must not be locked out of the reset that would fix it."""
        from utils.tasks import send_email_task

        self._clear_sender()
        send_email_task(
            subject="Your OTP",
            message="123456",
            from_email="platform@example.com",
            recipient_list=["someone@example.com"],
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, "platform@example.com")


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingSenderResolutionTests(TestCase):
    """`TenantSettings.booking_sender` re-checks everything rather than trusting
    the stored pointer."""

    def setUp(self):
        self.tenant = f.make_tenant()

    def test_a_tenant_with_no_settings_row_resolves_to_none(self):
        bare = f.make_tenant(with_email_sender=False)
        self.assertIsNone(TenantSettings.booking_sender(bare.id))

    def test_a_soft_deleted_configuration_resolves_to_none(self):
        config = TenantSettings.booking_sender(self.tenant.id)
        config.soft_delete()
        self.assertIsNone(TenantSettings.booking_sender(self.tenant.id))

    def test_another_tenants_configuration_is_never_resolved(self):
        """Belt and braces against a pointer written by a path that skipped
        validation: the lookup is scoped by tenant as well as by id."""
        other = f.make_tenant()
        foreign = TenantSettings.booking_sender(other.id)

        TenantSettings.objects.filter(tenant=self.tenant).update(
            booking_email_configuration=foreign
        )
        self.assertIsNone(TenantSettings.booking_sender(self.tenant.id))
