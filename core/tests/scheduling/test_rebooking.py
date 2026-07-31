import uuid
from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models.booking import Booking
from core.services import bookings as booking_service, rebooking
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


class _RebookBase(TestCase):
    def setUp(self):
        cache.clear()  # reset throttle state
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()
        original = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self.booking = booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=original,
            participants=[{"name": "Cust", "email": "cust@example.com"}],
            target_host_id=self.host.id,
            now=self.now,
        )
        self.token = uuid.uuid4()
        Booking.objects.filter(pk=self.booking.pk).update(
            status=Booking.Status.REQUIRES_RESCHEDULE,
            reschedule_token=self.token,
            reschedule_token_expires_at=self.now + timedelta(days=3),
        )
        self.new_start = (self.now + timedelta(days=2)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )


@REQUIRES_PG
class RebookingServiceTests(_RebookBase):
    def test_listed_slots_are_for_the_host_that_will_be_booked(self):
        """Slots offered on the reschedule page must be bookable by the original
        host — otherwise the customer picks a time only a colleague is free for
        and the submit 409s."""
        colleague = f.make_host(self.tenant, self.template)
        # Block the original host at 14:00 on the target day; the colleague is free.
        f.make_exception(
            self.tenant,
            self.host,
            self.new_start - timedelta(minutes=30),
            self.new_start + timedelta(hours=1),
        )
        day = self.new_start.date()
        _, slots = rebooking.list_slots(str(self.token), day, day, now=self.now)

        starts = {s.start for s in slots}
        self.assertNotIn(self.new_start, starts)
        for slot in slots:
            self.assertEqual(slot.eligible_host_ids, (self.host.id,))
            self.assertNotIn(colleague.id, slot.eligible_host_ids)

    def test_rebook_creates_new_and_retires_old(self):
        new = rebooking.rebook(str(self.token), self.new_start)
        self.assertEqual(new.status, Booking.Status.SCHEDULED)
        self.assertEqual(new.start_time, self.new_start)
        self.assertTrue(new.participants.filter(email="cust@example.com").exists())

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.RESCHEDULED)
        self.assertIsNone(self.booking.reschedule_token)  # consumed

    def test_token_is_single_use(self):
        rebooking.rebook(str(self.token), self.new_start)
        with self.assertRaises(rebooking.InvalidRescheduleToken):
            rebooking.rebook(str(self.token), self.new_start + timedelta(hours=1))

    def test_expired_token_rejected(self):
        Booking.objects.filter(pk=self.booking.pk).update(
            reschedule_token_expires_at=self.now - timedelta(minutes=1)
        )
        with self.assertRaises(rebooking.InvalidRescheduleToken):
            rebooking.get_context(str(self.token))

    def test_unknown_token_rejected(self):
        with self.assertRaises(rebooking.InvalidRescheduleToken):
            rebooking.get_context(str(uuid.uuid4()))


@REQUIRES_PG
class PublicRescheduleApiTests(_RebookBase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.url = f"/api/v1/core/public/reschedule/{self.token}/"

    def test_get_returns_slots(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"]["booking"]["event_type"], self.template.name
        )
        self.assertIsInstance(response.data["data"]["slots"], list)

    def test_post_rebooks_and_consumes_token(self):
        response = self.client.post(
            self.url, {"start_time": self.new_start.isoformat()}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.RESCHEDULED)

        # Token now consumed -> second attempt fails.
        again = self.client.post(
            self.url, {"start_time": self.new_start.isoformat()}, format="json"
        )
        self.assertEqual(again.status_code, 400)

    def test_unknown_token_400(self):
        response = self.client.get(
            f"/api/v1/core/public/reschedule/{uuid.uuid4()}/"
        )
        self.assertEqual(response.status_code, 400)

    def test_post_expired_token_400(self):
        Booking.objects.filter(pk=self.booking.pk).update(
            reschedule_token_expires_at=self.now - timedelta(minutes=1)
        )
        response = self.client.post(
            self.url, {"start_time": self.new_start.isoformat()}, format="json"
        )
        self.assertEqual(response.status_code, 400)
