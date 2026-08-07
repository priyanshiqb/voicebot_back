"""
The `sender` column on the notification log admin.

Null in `email_configuration` is not one condition but three — FCM never has a
sender, rows written before the column existed have none, and a message that was
never sent has none because the tenant assigned no booking sender. Only the last
is actionable, and it is the single most common reason booking mail is missing,
so the column has to say which one it is rather than showing an empty cell for
all three.
"""

from django.contrib import admin
from django.test import TestCase

from core.admin.notification_log_admin import NotificationLogAdmin
from core.models.notification_log import NotificationLog
from core.tests.scheduling import factories as f


class SenderColumnTests(TestCase):
    def setUp(self):
        self.admin = NotificationLogAdmin(NotificationLog, admin.site)
        self.tenant = f.make_tenant(with_email_sender=False)

    def _log(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "notification_type": NotificationLog.NotificationType.CONFIRMATION,
            "channel": NotificationLog.Channel.EMAIL,
            "recipient": "customer@example.com",
            "dedup_key": f"key-{overrides.pop('key', '1')}",
            "status": NotificationLog.Status.SENT,
        }
        fields.update(overrides)
        return NotificationLog.objects.create(**fields)

    def test_it_names_the_configuration_that_carried_the_message(self):
        config = f.make_email_sender(self.tenant)
        log = self._log(email_configuration=config)

        self.assertEqual(self.admin.sender(log), config.name)

    def test_a_failed_email_with_no_configuration_says_so(self):
        """The actionable case: booking mail is off because nothing is assigned."""
        log = self._log(status=NotificationLog.Status.FAILED)

        self.assertEqual(self.admin.sender(log), "no sender configured")

    def test_fcm_is_not_reported_as_a_missing_sender(self):
        """Push notifications never go through SMTP; flagging them would bury the
        rows that genuinely need attention."""
        log = self._log(
            channel=NotificationLog.Channel.FCM,
            status=NotificationLog.Status.FAILED,
        )

        self.assertEqual(self.admin.sender(log), "—")

    def test_a_sent_message_predating_the_column_is_left_blank(self):
        """Historical rows have no configuration recorded and nothing to fix."""
        log = self._log(status=NotificationLog.Status.SENT)

        self.assertEqual(self.admin.sender(log), "—")
