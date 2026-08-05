"""
`manage.py audit_bookings`.

An auditor that reports nothing is indistinguishable from a broken one, so every
test here PLANTS the violation and asserts the specific check fires. The clean
run at the top is what proves the checks are not simply always-on.

Violations are written with `all_objects` / raw updates on purpose: the whole
point of the command is to catch rows that bypassed the write path, and rows
created through the service layer cannot be wrong.
"""

from datetime import timedelta
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models.availability_profile import AvailabilityProfile
from core.models.availability_window import AvailabilityWindow
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.notification_log import NotificationLog
from core.services import bookings as booking_service
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


def run_audit(**kwargs):
    """Returns (output, error_or_None)."""
    out = StringIO()
    try:
        call_command("audit_bookings", stdout=out, stderr=out, quiet=True, **kwargs)
        return out.getvalue(), None
    except CommandError as exc:
        return out.getvalue(), str(exc)


@REQUIRES_PG
class AuditCleanSystemTests(TestCase):
    """A system built entirely through the service layer must be clean. If this
    fails, a check is reporting normal state and the whole command is noise."""

    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()

    def test_clean_system_reports_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=(self.now + timedelta(days=1)).replace(
                    hour=10, minute=0, second=0, microsecond=0
                ),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=self.now,
            )

        output, error = run_audit()
        self.assertIsNone(error, output)
        self.assertIn("OK", output)

    def test_a_live_hold_is_not_flagged(self):
        """A hold inside its TTL is normal; only a stranded one is a finding."""
        booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=(self.now + timedelta(days=1)).replace(
                hour=14, minute=0, second=0, microsecond=0
            ),
            participants=ATTENDEES,
            target_host_id=self.host.id,
            hold_ttl_seconds=600,
            now=self.now,
        )
        output, error = run_audit()
        self.assertIsNone(error, output)


@REQUIRES_PG
class AuditFindingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def _booking(self, **overrides):
        fields = dict(
            tenant=self.tenant,
            event_template=self.template,
            title="T",
            primary_host=self.host,
            start_time=self.start,
            end_time=self.start + timedelta(minutes=30),
            effective_start_time=self.start,
            effective_end_time=self.start + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
        )
        fields.update(overrides)
        booking = Booking.objects.create(**fields)
        BookingParticipant.objects.create(
            tenant=self.tenant,
            booking=booking,
            name="C",
            email="c@example.com",
            role=BookingParticipant.Role.ATTENDEE,
        )
        return booking

    def test_detects_effective_times_not_bracketing(self):
        """The half Postgres does not enforce: it checks each window is internally
        ordered, not that effective_* contains start/end."""
        booking = self._booking()
        Booking.objects.filter(pk=booking.pk).update(
            effective_start_time=self.start + timedelta(minutes=5)
        )
        output, error = run_audit(check="effective-times")
        self.assertIsNotNone(error)
        self.assertIn("effective-times", output)
        self.assertIn(str(booking.id), output)

    def test_detects_effective_end_short_of_end(self):
        booking = self._booking()
        Booking.objects.filter(pk=booking.pk).update(
            effective_end_time=self.start + timedelta(minutes=20)
        )
        output, error = run_audit(check="effective-times")
        self.assertIsNotNone(error)
        self.assertIn(str(booking.id), output)

    def test_detects_cross_tenant_template(self):
        other = f.make_tenant()
        booking = self._booking()
        Booking.objects.filter(pk=booking.pk).update(tenant=other)
        output, error = run_audit(check="tenant-coherence")
        self.assertIsNotNone(error)
        self.assertIn("cross-tenant-template", output)

    def test_detects_participantless_booking(self):
        booking = Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="T",
            primary_host=self.host,
            start_time=self.start,
            end_time=self.start + timedelta(minutes=30),
            effective_start_time=self.start,
            effective_end_time=self.start + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
        )
        output, error = run_audit(check="no-participants")
        self.assertIsNotNone(error)
        self.assertIn(str(booking.id), output)

    def test_cancelled_booking_without_participants_is_ignored(self):
        """Cancelled rows notify nobody by design — flagging them is noise."""
        Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="T",
            primary_host=self.host,
            start_time=self.start,
            end_time=self.start + timedelta(minutes=30),
            effective_start_time=self.start,
            effective_end_time=self.start + timedelta(minutes=30),
            status=Booking.Status.CANCELED,
        )
        output, error = run_audit(check="no-participants")
        self.assertIsNone(error, output)

    def test_detects_stranded_hold(self):
        booking = self._booking(
            status=Booking.Status.HELD,
            hold_expires_at=self.now - timedelta(hours=1),
        )
        output, error = run_audit(check="stranded-hold")
        self.assertIsNotNone(error)
        self.assertIn(str(booking.id), output)
        self.assertIn("cleanup_expired_holds", output)

    def test_detects_unmaterialized_profile(self):
        """A host with rules and no future windows — the exact state a dead
        materialization task produces."""
        AvailabilityWindow.all_objects.filter(user=self.host).delete()
        output, error = run_audit(check="unmaterialized-profile")
        self.assertIsNotNone(error)
        self.assertIn("materialize_availability", output)

    def test_detects_orphan_windows_from_a_deactivated_profile(self):
        profile = AvailabilityProfile.objects.get(user=self.host)
        AvailabilityProfile.objects.filter(pk=profile.pk).update(is_active=False)
        output, error = run_audit(check="orphan-window")
        self.assertIsNotNone(error)
        self.assertIn("orphan-window", output)

    def test_detects_closing_horizon(self):
        profile = AvailabilityProfile.objects.get(user=self.host)
        AvailabilityWindow.all_objects.filter(
            profile=profile, end_utc__gt=timezone.now() + timedelta(days=3)
        ).delete()
        output, error = run_audit(check="closing-horizon")
        self.assertIsNotNone(error)
        self.assertIn("closing-horizon", output)

    def test_healthy_horizon_is_not_flagged(self):
        output, error = run_audit(check="closing-horizon")
        self.assertIsNone(error, output)

    def test_detects_stuck_pending_notification(self):
        booking = self._booking()
        log = NotificationLog.objects.create(
            tenant=self.tenant,
            booking=booking,
            notification_type=NotificationLog.NotificationType.CONFIRMATION,
            channel=NotificationLog.Channel.EMAIL,
            recipient="c@example.com",
            dedup_key=f"stuck-{booking.id}",
            status=NotificationLog.Status.PENDING,
        )
        NotificationLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        output, error = run_audit(check="stuck-notification")
        self.assertIsNotNone(error)
        self.assertIn("stuck-notification", output)

    def test_recent_pending_notification_is_not_flagged(self):
        """In-flight is normal; only long-stuck rows mean the task died."""
        booking = self._booking()
        NotificationLog.objects.create(
            tenant=self.tenant,
            booking=booking,
            notification_type=NotificationLog.NotificationType.CONFIRMATION,
            channel=NotificationLog.Channel.EMAIL,
            recipient="c@example.com",
            dedup_key=f"fresh-{booking.id}",
            status=NotificationLog.Status.PENDING,
        )
        output, error = run_audit(check="stuck-notification")
        self.assertIsNone(error, output)


@REQUIRES_PG
class SchemaGuardTests(TestCase):
    """The constraints are what make most invariants unreachable, so the audit's
    job is to confirm they are present — not to re-derive what they guarantee."""

    def test_all_required_constraints_are_present(self):
        output, error = run_audit(check="schema-guards")
        self.assertIsNone(error, output)

    def test_a_missing_constraint_is_reported(self):
        from core.management.commands import audit_bookings as cmd

        with patch.dict(
            cmd.REQUIRED_CONSTRAINTS,
            {"constraint_that_does_not_exist": "the sky falls"},
        ):
            output, error = run_audit(check="schema-guards")

        self.assertIsNotNone(error)
        self.assertIn("constraint_that_does_not_exist", output)
        self.assertIn("the sky falls", output)


@REQUIRES_PG
class AuditScopingTests(TestCase):
    def setUp(self):
        self.tenant_a = f.make_tenant()
        self.tenant_b = f.make_tenant()
        self.template_b = f.make_template(self.tenant_b)
        self.host_b = f.make_host(self.tenant_b, self.template_b)
        # Break tenant B only.
        AvailabilityWindow.all_objects.filter(user=self.host_b).delete()

    def test_tenant_filter_excludes_other_tenants_problems(self):
        output, error = run_audit(tenant=str(self.tenant_a.id))
        self.assertIsNone(error, output)

    def test_tenant_filter_finds_its_own(self):
        output, error = run_audit(tenant=str(self.tenant_b.id))
        self.assertIsNotNone(error)
        self.assertIn("unmaterialized-profile", output)

    def test_unknown_tenant_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command(
                "audit_bookings", tenant="00000000-0000-0000-0000-000000000000"
            )

    def test_unknown_check_name_is_an_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("audit_bookings", check="does-not-exist")
        self.assertIn("Available:", str(ctx.exception))
