"""
Operational polish: failures that were survivable but invisible or self-inflicted.

  P1-5  calendar tasks retried permanent errors (revoked token, deleted event)
        three times and then abandoned them silently, leaving external_event_id
        pointing at nothing so every later push failed forever.
  P1-6  .ics SEQUENCE was hard-coded to 0, so RFC 5545 lets a client ignore an
        updated invite for the same UID — a reschedule left the old time in place.
  P2-1  beat loops materialized everything and aborted as a batch: one bad row
        stranded every later one, and the next run hit the same row again.
  P2-3  the reschedule-token lookup picked a group member non-deterministically.
"""

from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.calendar_integration import CalendarIntegration
from core.services import bookings as booking_service, calendars, notifications
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


class FakeResp:
    def __init__(self, status):
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = FakeResp(status)


class ErrorClassificationTests(TestCase):
    def test_auth_and_gone_are_permanent(self):
        for status in (400, 401, 403, 404, 410, 422):
            self.assertTrue(
                calendars.is_permanent(FakeHttpError(status)), f"{status} should be permanent"
            )

    def test_server_errors_and_rate_limits_are_transient(self):
        for status in (408, 429, 500, 502, 503, 504):
            self.assertFalse(
                calendars.is_permanent(FakeHttpError(status)), f"{status} should retry"
            )

    def test_unknown_shapes_are_treated_as_transient(self):
        """No status means we cannot tell — assume transient and let retries decide,
        rather than permanently disabling an integration on a network blip."""
        self.assertFalse(calendars.is_permanent(OSError("connection reset")))
        self.assertIsNone(calendars.http_status_of(OSError("connection reset")))

    def test_status_code_attribute_is_also_read(self):
        exc = type("E", (Exception,), {"status_code": 401})()
        self.assertEqual(calendars.http_status_of(exc), 401)


@REQUIRES_PG
class CalendarPermanentErrorTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self.integration = CalendarIntegration(
            tenant=self.tenant,
            user=self.host,
            provider=CalendarIntegration.Provider.GOOGLE,
            account_email="host@gmail.com",
            sync_status=CalendarIntegration.SyncStatus.CONNECTED,
        )
        self.integration.set_tokens(access_token="a", refresh_token="r")
        self.integration.save()
        self.booking = Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="T",
            primary_host=self.host,
            start_time=self.start,
            end_time=self.start + timedelta(minutes=30),
            effective_start_time=self.start,
            effective_end_time=self.start + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
            external_event_id="ext-1",
        )

    @patch("core.services.calendars.get_provider_for")
    def test_revoked_token_flags_reauth_and_does_not_retry(self, provider):
        from core.tasks import push_booking_to_calendar

        provider.return_value.update_event.side_effect = FakeHttpError(401)
        with self.assertLogs("core.tasks", level="ERROR"):
            push_booking_to_calendar(str(self.booking.id))

        self.integration.refresh_from_db()
        self.assertEqual(
            self.integration.sync_status,
            CalendarIntegration.SyncStatus.REAUTH_REQUIRED,
        )
        self.assertIn("401", self.integration.last_error)

    @patch("core.services.calendars.get_provider_for")
    def test_deleted_remote_event_clears_id_for_repush(self, provider):
        """404 used to be retried and abandoned, leaving external_event_id set — so
        the next push took the update branch and failed forever."""
        from core.tasks import push_booking_to_calendar

        provider.return_value.update_event.side_effect = FakeHttpError(404)
        with self.assertLogs("core.tasks", level="WARNING"):
            push_booking_to_calendar(str(self.booking.id))

        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.external_event_id)
        # A permanent-but-recoverable error must NOT disable the integration.
        self.integration.refresh_from_db()
        self.assertEqual(
            self.integration.sync_status, CalendarIntegration.SyncStatus.CONNECTED
        )

    @patch("core.services.calendars.get_provider_for")
    def test_transient_error_still_retries(self, provider):
        """A 5xx must take the retry branch, not the permanent one.

        Invoked directly rather than via .delay(), so `self.retry()` re-raises the
        original exception instead of `Retry` — there is no request context to
        schedule against. Raising at all is the assertion that matters: the
        permanent branch returns without raising.
        """
        from core.tasks import push_booking_to_calendar

        provider.return_value.update_event.side_effect = FakeHttpError(503)
        with self.assertRaises(FakeHttpError):
            push_booking_to_calendar(str(self.booking.id))

        self.integration.refresh_from_db()
        self.assertEqual(
            self.integration.sync_status, CalendarIntegration.SyncStatus.CONNECTED
        )
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.external_event_id, "ext-1")  # not cleared


@REQUIRES_PG
class IcsSequenceTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def _sequence_from_outbox(self):
        blob = next(
            att[1] for att in mail.outbox[-1].attachments if att[0] == "invite.ics"
        )
        line = next(ln for ln in blob.splitlines() if ln.startswith("SEQUENCE:"))
        return int(line.split(":", 1)[1])

    def test_sequence_increases_across_revisions(self):
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=self.start,
                participants=[{"name": "C", "email": "c@example.com"}],
                target_host_id=self.host.id,
                now=self.now,
            )
        first = self._sequence_from_outbox()
        self.assertGreaterEqual(first, 1)  # CREATED already counted

        with self.captureOnCommitCallbacks(execute=True):
            booking_service.cancel_booking(
                booking=booking, enforce_lead=False, now=self.now
            )
        second = self._sequence_from_outbox()
        self.assertGreater(
            second, first, "SEQUENCE must increase or clients ignore the update"
        )


@REQUIRES_PG
class BeatIsolationTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()

    def _hold(self, hour):
        start = (self.now + timedelta(days=1)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        return booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=[{"name": "C", "email": f"c{hour}@example.com"}],
            target_host_id=self.host.id,
            hold_ttl_seconds=1,
            now=self.now,
        )

    def test_one_failing_hold_does_not_strand_the_rest(self):
        """Previously a raise mid-loop aborted the run, leaving every later hold
        reserved — and the next run hit the same row again."""
        holds = [self._hold(9), self._hold(11), self._hold(13)]
        later = self.now + timedelta(minutes=5)

        real_log_event = booking_service.events.log_event
        failing_id = holds[1].id

        def flaky(booking, *args, **kwargs):
            if booking.id == failing_id:
                raise RuntimeError("boom")
            return real_log_event(booking, *args, **kwargs)

        with patch.object(booking_service.events, "log_event", side_effect=flaky):
            with self.assertLogs("core.services.bookings", level="ERROR"):
                released = booking_service.release_expired_holds(now=later)

        self.assertEqual(released, 2)
        statuses = {
            b.id: Booking.objects.get(pk=b.id).status for b in holds
        }
        self.assertEqual(statuses[holds[0].id], Booking.Status.CANCELED)
        self.assertEqual(statuses[holds[2].id], Booking.Status.CANCELED)
        # The failing one is untouched and will be retried next run.
        self.assertEqual(statuses[failing_id], Booking.Status.HELD)

    def test_one_failing_reminder_does_not_stop_the_scan(self):
        from core.services import reminders

        # Spaced an hour apart: same host, and 30-minute bookings any closer
        # collide on the overlap exclusion constraint.
        starts = [self.now + timedelta(minutes=30 + i * 60) for i in range(3)]
        for i, start in enumerate(starts):
            booking = Booking.objects.create(
                tenant=self.tenant,
                event_template=self.template,
                title=f"B{i}",
                primary_host=self.host,
                start_time=start,
                end_time=start + timedelta(minutes=30),
                effective_start_time=start,
                effective_end_time=start + timedelta(minutes=30),
                status=Booking.Status.SCHEDULED,
            )
            # Required: _emit short-circuits on an empty recipient list, so a
            # participant-less booking never counts as a send.
            BookingParticipant.objects.create(
                tenant=self.tenant,
                booking=booking,
                name=f"C{i}",
                email=f"c{i}@example.com",
                role=BookingParticipant.Role.ATTENDEE,
            )

        calls = {"n": 0}
        real = notifications.send_reminder

        def flaky(booking_id, offset):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real(booking_id, offset)

        with patch.object(reminders.notifications, "send_reminder", side_effect=flaky):
            with self.assertLogs("core.services.reminders", level="ERROR"):
                sent = reminders.send_due_reminders(now=self.now)

        self.assertEqual(calls["n"], 3, "scan stopped early instead of isolating")
        self.assertEqual(sent, 2)
