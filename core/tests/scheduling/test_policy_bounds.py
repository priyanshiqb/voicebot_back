"""
Bounds that keep a synchronous booking request finite, and the invariants that
back them at the database level.

These exist because the field types alone permit values that let a tenant DoS
itself through the ordinary API: `max_horizon_days` is a PositiveSmallIntegerField
(so up to 32767) and slot granularity was floored at 1 minute, making slot
generation ~47 million iterations per host per request.
"""

from datetime import timedelta
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.event_template import (
    MAX_HORIZON_DAYS,
    MIN_SLOT_GRANULARITY_MINUTES,
    EventTemplate,
)
from core.serializers.booking import MAX_PARTICIPANTS_PER_BOOKING
from core.services import bookings as booking_service
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class EventTemplateBoundsTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()

    def _template(self, **kwargs):
        return EventTemplate(
            tenant=self.tenant,
            name="Bounded",
            duration_minutes=30,
            **kwargs,
        )

    def test_horizon_above_cap_rejected_by_validators(self):
        with self.assertRaises(ValidationError) as ctx:
            self._template(max_horizon_days=MAX_HORIZON_DAYS + 1).full_clean(
                exclude=["created_by", "updated_by", "deleted_by"]
            )
        self.assertIn("max_horizon_days", ctx.exception.error_dict)

    def test_horizon_zero_rejected(self):
        with self.assertRaises(ValidationError):
            self._template(max_horizon_days=0).full_clean(
                exclude=["created_by", "updated_by", "deleted_by"]
            )

    def test_granularity_below_floor_rejected_by_validators(self):
        with self.assertRaises(ValidationError) as ctx:
            self._template(
                slot_granularity_minutes=MIN_SLOT_GRANULARITY_MINUTES - 1
            ).full_clean(exclude=["created_by", "updated_by", "deleted_by"])
        self.assertIn("slot_granularity_minutes", ctx.exception.error_dict)

    def test_horizon_cap_enforced_by_database(self):
        """Validators only run in full_clean(); the constraint covers direct ORM writes."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventTemplate.objects.create(
                tenant=self.tenant,
                name="Unbounded horizon",
                duration_minutes=30,
                max_horizon_days=32767,
            )

    def test_granularity_floor_enforced_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventTemplate.objects.create(
                tenant=self.tenant,
                name="One minute grid",
                duration_minutes=30,
                slot_granularity_minutes=1,
            )

    def test_values_at_the_boundary_are_accepted(self):
        template = self._template(
            max_horizon_days=MAX_HORIZON_DAYS,
            slot_granularity_minutes=MIN_SLOT_GRANULARITY_MINUTES,
        )
        template.full_clean(exclude=["created_by", "updated_by", "deleted_by"])
        template.save()
        self.assertIsNotNone(template.pk)


@REQUIRES_PG
class HeldBookingConstraintTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def test_held_without_expiry_rejected_by_database(self):
        """A HELD row occupies the overlap exclusion constraint. Without an expiry
        it reserves its slot forever — release_expired_holds filters on
        hold_expires_at__lt and would never see it."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(
                tenant=self.tenant,
                event_template=self.template,
                title="Orphan hold",
                primary_host=self.host,
                start_time=self.start,
                end_time=self.start + timedelta(minutes=30),
                effective_start_time=self.start,
                effective_end_time=self.start + timedelta(minutes=30),
                status=Booking.Status.HELD,
                hold_expires_at=None,
            )

    def test_service_always_sets_expiry_on_hold(self):
        hold = booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=[{"name": "C", "email": "c@example.com"}],
            target_host_id=self.host.id,
            now=self.now,
        )
        self.assertEqual(hold.status, Booking.Status.HELD)
        self.assertIsNotNone(hold.hold_expires_at)

    def test_confirming_a_hold_clears_expiry_without_tripping_constraint(self):
        hold = booking_service.hold_slot(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=[{"name": "C", "email": "c@example.com"}],
            target_host_id=self.host.id,
            now=self.now,
        )
        confirmed = booking_service.confirm_hold(booking=hold, now=self.now)
        self.assertEqual(confirmed.status, Booking.Status.CONFIRMED)
        self.assertIsNone(confirmed.hold_expires_at)

    def test_non_held_statuses_may_have_no_expiry(self):
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=[{"name": "C", "email": "c@example.com"}],
            target_host_id=self.host.id,
            now=self.now,
        )
        self.assertIsNone(booking.hold_expires_at)


@REQUIRES_PG
class ParticipantLimitTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        self.start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

    def _payload(self, count):
        return [
            {"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(count)
        ]

    def test_over_limit_rejected(self):
        from core.serializers.booking import BookingCreateSerializer

        serializer = BookingCreateSerializer(
            data={
                "event_template": str(self.template.id),
                "start_time": self.start.isoformat(),
                "target_host": str(self.host.id),
                "participants": self._payload(MAX_PARTICIPANTS_PER_BOOKING + 1),
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("participants", serializer.errors)

    def test_at_limit_accepted_and_bulk_inserted(self):
        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=self._payload(MAX_PARTICIPANTS_PER_BOOKING),
            target_host_id=self.host.id,
            now=self.now,
        )
        # host row + every attendee
        self.assertEqual(
            booking.participants.count(), MAX_PARTICIPANTS_PER_BOOKING + 1
        )
        self.assertEqual(
            booking.participants.filter(
                role=BookingParticipant.Role.HOST
            ).count(),
            1,
        )

    def test_bulk_create_still_resolves_contacts(self):
        """bulk_create skips pre_save, so contact resolution is applied explicitly.
        Losing it would silently stop linking attendees to Contacts by phone —
        a regression with no error, only missing links noticed much later."""
        from core.models.contact import Contact

        contact = Contact.objects.create(
            tenant=self.tenant,
            first_name="Known",
            phone_number="+14155550123",
            phone_e164="+14155550123",
        )

        booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=self.start,
            participants=[
                {
                    "name": "Known",
                    "email": "known@example.com",
                    "phone_number": "+14155550123",
                }
            ],
            target_host_id=self.host.id,
            now=self.now,
        )
        attendee = booking.participants.get(email="known@example.com")
        self.assertEqual(attendee.contact_id, contact.id)
