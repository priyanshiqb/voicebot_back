from datetime import timedelta
from unittest import skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill
from core.services import bookings as booking_service, reschedule
from core.services.exceptions import SlotConflictError
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class CollectiveBookingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, assignment_strategy=EventTemplate.AssignmentStrategy.COLLECTIVE
        )
        self.h1 = f.make_host(self.tenant, self.template)
        self.h2 = f.make_host(self.tenant, self.template)
        self.h3 = f.make_host(self.tenant, self.template)

    def _book(self):
        return booking_service.create_collective_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            now=NOW,
        )

    def test_books_all_hosts_one_group(self):
        created = self._book()
        self.assertEqual(len(created), 3)
        self.assertEqual(len({b.group_id for b in created}), 1)
        self.assertIsNotNone(created[0].group_id)
        self.assertEqual(
            {b.primary_host_id for b in created},
            {self.h1.id, self.h2.id, self.h3.id},
        )
        self.assertEqual(
            Booking.objects.filter(status=Booking.Status.SCHEDULED).count(), 3
        )

    def test_all_or_nothing_when_one_host_busy(self):
        # Make h2 busy at the slot via a separate DIRECT booking.
        other = f.make_template(self.tenant)
        UserEventSkill.objects.create(
            tenant=self.tenant, user=self.h2, event_template=other
        )
        booking_service.create_booking(
            tenant=self.tenant,
            event_template=other,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=self.h2.id,
            now=NOW,
        )
        with self.assertRaises(SlotConflictError):
            self._book()
        # Nothing from the collective template was created.
        self.assertEqual(
            Booking.objects.filter(event_template=self.template).count(), 0
        )

    def test_group_confirmation_sent_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._book()
        self.assertEqual(len(mail.outbox), 1)
        recipients = set(mail.outbox[0].to)
        self.assertIn("customer@example.com", recipients)
        self.assertEqual(len(recipients), 4)  # 3 hosts + 1 attendee

    def test_leave_flags_whole_group(self):
        created = self._book()
        leave = f.make_exception(self.tenant, self.h1, at(9), at(12))

        result = reschedule.handle_leave(self.tenant, leave.id, now=NOW)

        self.assertEqual(result["flagged"], 1)  # one leaving-host row processed…
        for booking in created:  # …but the whole meeting is flagged
            booking.refresh_from_db()
            self.assertEqual(booking.status, Booking.Status.REQUIRES_RESCHEDULE)
        self.assertEqual(len({b.reschedule_token for b in created}), 1)  # shared token

    def test_cancel_group(self):
        created = self._book()
        canceled = booking_service.cancel_group(
            group_id=created[0].group_id, enforce_lead=False, now=NOW
        )
        self.assertEqual(len(canceled), 3)
        for booking in created:
            booking.refresh_from_db()
            self.assertEqual(booking.status, Booking.Status.CANCELED)

    def test_api_collective_create_returns_list(self):
        actor = f.make_user(self.tenant)
        client = APIClient()
        client.force_authenticate(user=actor)
        start = (timezone.now() + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        response = client.post(
            "/api/v1/core/bookings/",
            {
                "event_template": str(self.template.id),
                "start_time": start.isoformat(),
                "participants": [{"name": "Cust", "email": "cust@example.com"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["data"]), 3)
