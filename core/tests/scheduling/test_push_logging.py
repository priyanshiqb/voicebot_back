"""
Push notifications are now recorded and deduplicated like email.

``NotificationLog`` carried an ``FCM`` channel that no production code ever wrote,
so a push had no dedup key, no audit row and no retry: a retried Celery task
re-pushed, a transient FCM failure dropped the alert silently, and "did the on-call
admin get told?" had no answer anywhere in the system.

The dedup-key collision test is the load-bearing one. Email and push describe the
same event and share one unique index on ``(tenant, dedup_key)`` — an unprefixed
key would make whichever channel fired first permanently suppress the other, and
the symptom would be a missing notification, not an error.
"""

from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase

from core.models.device_token import DeviceToken
from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service, notifications, push, reschedule
from core.tasks import send_push_to_admins_task
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class AdminPushLedgerTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        host = f.make_host(self.tenant, self.template)
        self.booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )
        if isinstance(self.booking, (list, tuple)):
            self.booking = self.booking[0]

    def emit(self):
        return notifications.emit_admin_push(
            self.booking,
            NotificationLog.NotificationType.RESCHEDULE_REQUEST,
            f"push:{self.booking.id}:RESCHEDULE_REQUEST",
            "Booking needs rescheduling",
            self.booking.title,
        )

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_a_push_writes_an_fcm_row(self, _send):
        self.assertTrue(self.emit())

        log = NotificationLog.objects.get(dedup_key__startswith="push:")
        self.assertEqual(log.channel, NotificationLog.Channel.FCM)
        self.assertEqual(log.status, NotificationLog.Status.SENT)
        self.assertEqual(log.booking_id, self.booking.id)
        self.assertIsNotNone(log.sent_at)

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_a_second_emit_for_the_same_event_is_a_no_op(self, send):
        self.assertTrue(self.emit())
        self.assertFalse(self.emit())
        self.assertEqual(send.call_count, 1)
        self.assertEqual(NotificationLog.objects.filter(channel="FCM").count(), 1)

    @patch("core.services.push.notify_tenant_admins", return_value=0)
    def test_nothing_accepted_is_recorded_as_failed_not_sent(self, _send):
        """Zero accepted is not success.

        Push may be unconfigured, or every device may have been retired. Marking the
        row SENT would claim a delivery that never happened *and* permanently
        suppress the retry, because a SENT row is never re-enqueued.
        """
        self.emit()

        log = NotificationLog.objects.get(channel="FCM")
        self.assertEqual(log.status, NotificationLog.Status.FAILED)
        self.assertIn("no live device tokens", log.error)

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_a_failed_row_can_be_re_emitted(self, send):
        with patch("core.services.push.notify_tenant_admins", return_value=0):
            self.emit()
        NotificationLog.objects.filter(channel="FCM").update(
            updated_at=NOW - notifications.PENDING_IN_FLIGHT_GRACE * 2
        )

        self.assertTrue(self.emit())
        self.assertEqual(
            NotificationLog.objects.get(channel="FCM").status,
            NotificationLog.Status.SENT,
        )

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_the_push_key_does_not_collide_with_the_email_key(self, _send):
        """Both channels notify about the same booking event.

        `(tenant, dedup_key)` is unique, so if the push reused the email's key the
        second channel would silently never fire.
        """
        self.emit()
        with patch(
            "core.tasks.send_scheduling_email_task.delay"
        ), patch(
            "tenant.models.TenantSettings.TenantSettings.booking_sender",
            return_value=object(),
        ):
            notifications.send_reschedule_request(self.booking.id)

        keys = set(NotificationLog.objects.values_list("dedup_key", flat=True))
        self.assertTrue(any(k.startswith("push:") for k in keys))
        self.assertTrue(any(not k.startswith("push:") for k in keys))
        self.assertEqual(NotificationLog.objects.filter(channel="FCM").count(), 1)


@REQUIRES_PG
class PushTaskRetryTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.log = NotificationLog.objects.create(
            tenant=self.tenant,
            channel=NotificationLog.Channel.FCM,
            notification_type=NotificationLog.NotificationType.RESCHEDULE_REQUEST,
            recipient="fcm:tenant-admins",
            dedup_key="push:test:1",
            status=NotificationLog.Status.PENDING,
        )

    def run_task(self, **options):
        return send_push_to_admins_task.apply(
            kwargs={
                "tenant_id": str(self.tenant.id),
                "title": "t",
                "body": "b",
                "notification_log_id": str(self.log.id),
            },
            **options,
        )

    @patch("core.services.push.notify_tenant_admins", return_value=2)
    def test_a_successful_send_marks_the_row_sent(self, _send):
        self.run_task()
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, NotificationLog.Status.SENT)

    def test_a_failure_with_retries_left_asks_for_a_retry(self):
        """The task must not swallow — a swallowed failure never retries.

        Push had no retry at all before this; the point of the change is that a
        transient FCM failure gets another attempt rather than dropping an
        operational alert on the floor.

        ``retry`` is stubbed with a plain exception rather than Celery's ``Retry``
        because eager mode handles a real ``Retry`` by re-invoking the task, which
        would recurse to exhaustion and test the wrong branch.
        """

        class RetryRequested(Exception):
            pass

        with patch(
            "core.services.push.notify_tenant_admins",
            side_effect=RuntimeError("fcm exploded"),
        ), patch.object(
            send_push_to_admins_task, "retry", side_effect=RetryRequested()
        ) as retry:
            self.run_task(throw=False)

        self.assertTrue(retry.called)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, NotificationLog.Status.PENDING)

    @patch(
        "core.services.push.notify_tenant_admins",
        side_effect=RuntimeError("fcm exploded"),
    )
    def test_the_last_attempt_records_the_failure(self, _send):
        self.run_task(retries=3)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, NotificationLog.Status.FAILED)
        self.assertIn("fcm exploded", self.log.error)


@REQUIRES_PG
class ReschedulePushEndToEndTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_flagging_a_booking_records_and_deep_links_the_push(self, send):
        host = f.make_host(self.tenant, self.template)
        booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )
        leave = f.make_exception(self.tenant, host, at(9), at(12))

        with self.captureOnCommitCallbacks(execute=True):
            reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertTrue(send.called)
        envelope = send.call_args.kwargs
        self.assertIn("route", envelope)
        self.assertTrue(envelope["route"].startswith("bookings/"))
        self.assertIn("collapse_key", envelope)

        log = NotificationLog.objects.get(channel="FCM")
        self.assertTrue(log.dedup_key.startswith("push:"))
        self.assertEqual(log.status, NotificationLog.Status.SENT)

    @patch("core.services.push.notify_tenant_admins", return_value=1)
    def test_re_running_the_same_leave_does_not_push_twice(self, send):
        host = f.make_host(self.tenant, self.template)
        booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=host.id,
            now=NOW,
        )
        leave = f.make_exception(self.tenant, host, at(9), at(12))

        with self.captureOnCommitCallbacks(execute=True):
            reschedule.handle_leave(self.tenant, leave.id, now=NOW)
        with self.captureOnCommitCallbacks(execute=True):
            reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(NotificationLog.objects.filter(channel="FCM").count(), 1)


@REQUIRES_PG
class PurgeCommandTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)

    def make(self, token, days, **kwargs):
        from datetime import timedelta

        from django.utils import timezone

        row = DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token=token, **kwargs
        )
        DeviceToken.objects.filter(pk=row.pk).update(
            last_seen_at=timezone.now() - timedelta(days=days)
        )
        return row

    def call(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("purge_device_tokens", *args, stdout=out)
        return out.getvalue()

    def test_the_command_demotes_and_purges(self):
        self.make("stale", 70)
        self.make("dead", 100, is_active=False)

        output = self.call()
        self.assertIn("Deactivated 1", output)
        self.assertIn("deleted 1", output)
        self.assertFalse(DeviceToken.objects.get(token="stale").is_active)

    def test_dry_run_reports_without_changing_anything(self):
        self.make("stale", 70)
        self.make("dead", 100, is_active=False)

        output = self.call("--dry-run")
        self.assertIn("Would deactivate 1", output)
        self.assertTrue(DeviceToken.objects.get(token="stale").is_active)
        self.assertEqual(DeviceToken.all_objects.count(), 2)

    def test_verify_probes_before_acting(self):
        self.make("stale", 70)

        with patch.object(push, "probe_token", return_value=False) as probe:
            output = self.call("--verify")

        self.assertTrue(probe.called)
        self.assertIn("1 rejected by FCM", output)

    def test_the_command_and_the_beat_task_share_one_implementation(self):
        """A hand-run cleanup and the scheduled one must not drift.

        The command is a wrapper over ``device_tokens.sweep``; if it grew its own
        copy of the rules, an operator could get a different outcome from the same
        situation, which is the failure that makes people stop trusting the command.
        """
        from core.tasks import sweep_device_tokens

        self.make("stale", 70)
        self.make("dead", 100, is_active=False)

        self.assertEqual(sweep_device_tokens(), 2)
