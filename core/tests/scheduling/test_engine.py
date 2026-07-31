from datetime import timedelta
from unittest import skipUnless

from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from core.models.booking import Booking
from core.services import bookings as booking_service
from core.services.exceptions import (
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql",
    "Booking overlap guarantees require the PostgreSQL exclusion constraint.",
)


@REQUIRES_PG
class BookingEngineTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)

    def _book(self, template=None, start=None, host=None, **kwargs):
        return booking_service.create_booking(
            tenant=self.tenant,
            event_template=template or self.template,
            start_time=start or at(10),
            participants=ATTENDEES,
            target_host_id=(host or self.host).id if (host or self.host) else None,
            now=NOW,
            **kwargs,
        )

    def test_db_constraint_blocks_overlap(self):
        start = at(10)
        Booking.objects.create(
            tenant=self.tenant,
            event_template=self.template,
            title="A",
            primary_host=self.host,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            effective_start_time=start,
            effective_end_time=start + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Booking.objects.create(
                    tenant=self.tenant,
                    event_template=self.template,
                    title="B",
                    primary_host=self.host,
                    start_time=start + timedelta(minutes=15),
                    end_time=start + timedelta(minutes=45),
                    effective_start_time=start + timedelta(minutes=15),
                    effective_end_time=start + timedelta(minutes=45),
                    status=Booking.Status.SCHEDULED,
                )

    def test_service_second_booking_conflicts(self):
        self._book()
        with self.assertRaises(SlotConflictError):
            self._book()

    def test_back_to_back_allowed(self):
        self._book(start=at(10))
        self._book(start=at(10, 30))
        self.assertEqual(
            Booking.objects.filter(status=Booking.Status.SCHEDULED).count(), 2
        )

    def test_buffers_block_adjacent_but_allow_gap(self):
        tmpl = f.make_template(
            self.tenant,
            buffer_before_minutes=15,
            buffer_after_minutes=15,
            duration_minutes=30,
        )
        host = f.make_host(self.tenant, tmpl)
        # 10:00-10:30, effective 09:45-10:45
        self._book(template=tmpl, host=host, start=at(10))
        # 10:30-11:00, effective 10:15-11:15 -> overlaps buffer -> rejected
        with self.assertRaises(SlotConflictError):
            self._book(template=tmpl, host=host, start=at(10, 30))
        # 11:00-11:30, effective 10:45-11:15 -> touches boundary [) -> allowed
        ok = self._book(template=tmpl, host=host, start=at(11))
        self.assertEqual(ok.status, Booking.Status.SCHEDULED)

    def test_never_falls_back_to_a_free_colleague(self):
        """The named host is the only candidate: a free second host does not make
        the booked host's slot bookable twice."""
        second_host = f.make_host(self.tenant, self.template)
        first = self._book(start=at(10))
        self.assertEqual(first.primary_host_id, self.host.id)

        with self.assertRaises(SlotConflictError):
            self._book(start=at(10))

        # …while the colleague is bookable at that same time, when named.
        other = self._book(start=at(10), host=second_host)
        self.assertEqual(other.primary_host_id, second_host.id)

    def test_target_host_accepts_string_uuid(self):
        """The AI tool layer hands us JSON strings, not UUID objects."""
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=str(self.host.id),
            now=NOW,
        )
        self.assertEqual(booking.primary_host_id, self.host.id)

    def test_target_host_is_required(self):
        with self.assertRaises(NoEligibleHostError):
            booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                now=NOW,
            )

    def test_unskilled_target_host_rejected(self):
        """A user with a profile but no UserEventSkill for this template must not
        be bookable just by naming them."""
        other_template = f.make_template(self.tenant)
        stranger = f.make_host(self.tenant, other_template)
        with self.assertRaises(NoEligibleHostError):
            self._book(start=at(10), host=stranger)

    def test_cross_tenant_target_host_rejected(self):
        other_tenant = f.make_tenant()
        other_template = f.make_template(other_tenant)
        foreign_host = f.make_host(other_tenant, other_template)
        with self.assertRaises(NoEligibleHostError):
            self._book(start=at(10), host=foreign_host)

    def test_no_eligible_host(self):
        tmpl = f.make_template(self.tenant)  # no skills configured
        with self.assertRaises(NoEligibleHostError):
            booking_service.create_booking(
                tenant=self.tenant,
                event_template=tmpl,
                start_time=at(10),
                participants=ATTENDEES,
                now=NOW,
            )

    def test_min_notice_enforced(self):
        tmpl = f.make_template(self.tenant, min_notice_minutes=120)
        host = f.make_host(self.tenant, tmpl)
        with self.assertRaises(PolicyViolationError):
            self._book(template=tmpl, host=host, start=NOW + timedelta(minutes=30))

    def test_horizon_enforced(self):
        tmpl = f.make_template(self.tenant, max_horizon_days=7)
        host = f.make_host(self.tenant, tmpl)
        with self.assertRaises(PolicyViolationError):
            self._book(template=tmpl, host=host, start=NOW + timedelta(days=30))

    def test_past_rejected(self):
        with self.assertRaises(PolicyViolationError):
            self._book(start=NOW - timedelta(hours=1))

    def test_hold_blocks_then_confirm(self):
        start = at(10)
        hold = booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=self.host.id,
            now=NOW,
        )
        self.assertEqual(hold.status, Booking.Status.HELD)
        with self.assertRaises(SlotConflictError):
            self._book(start=start)
        confirmed = booking_service.confirm_hold(booking=hold, now=NOW)
        self.assertEqual(confirmed.status, Booking.Status.CONFIRMED)

    def test_expired_hold_released_frees_slot(self):
        start = at(10)
        hold = booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=self.host.id,
            now=NOW,
        )
        Booking.objects.filter(pk=hold.pk).update(
            hold_expires_at=NOW - timedelta(minutes=1)
        )
        self.assertEqual(booking_service.release_expired_holds(now=NOW), 1)
        hold.refresh_from_db()
        self.assertEqual(hold.status, Booking.Status.CANCELED)
        ok = self._book(start=start)
        self.assertEqual(ok.status, Booking.Status.SCHEDULED)

    def test_cancellation_lead_time(self):
        tmpl = f.make_template(self.tenant, cancellation_lead_minutes=60)
        host = f.make_host(self.tenant, tmpl)
        booking = self._book(
            template=tmpl, host=host, start=NOW + timedelta(minutes=30)
        )
        with self.assertRaises(PolicyViolationError):
            booking_service.cancel_booking(booking=booking, enforce_lead=True, now=NOW)
        canceled = booking_service.cancel_booking(
            booking=booking, enforce_lead=False, now=NOW
        )
        self.assertEqual(canceled.status, Booking.Status.CANCELED)

    def test_cancel_frees_slot(self):
        start = at(10)
        booking = self._book(start=start)
        booking_service.cancel_booking(booking=booking, enforce_lead=False, now=NOW)
        ok = self._book(start=start)
        self.assertEqual(ok.status, Booking.Status.SCHEDULED)

    def test_creates_booking_event_log(self):
        booking = self._book()
        self.assertTrue(booking.events.filter(event_type="CREATED").exists())
