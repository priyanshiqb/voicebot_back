from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from core.tests.scheduling import factories as f


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingAPITests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)
        self.bookings_url = "/api/v1/core/bookings/"
        self.slots_url = "/api/v1/core/availability/available-slots/"

    def _future_start(self):
        base = timezone.now() + timedelta(days=2)
        return base.replace(hour=10, minute=0, second=0, microsecond=0)

    def _payload(self):
        return {
            "event_template": str(self.template.id),
            "start_time": self._future_start().isoformat(),
            "participants": [{"name": "Cust", "email": "cust@example.com"}],
        }

    def test_create_returns_201(self):
        response = self.client.post(self.bookings_url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["status"], "SCHEDULED")

    def test_double_book_returns_409(self):
        payload = self._payload()
        first = self.client.post(self.bookings_url, payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(self.bookings_url, payload, format="json")
        self.assertEqual(second.status_code, 409, second.data)
        self.assertFalse(second.data["status"])

    def test_missing_participants_rejected(self):
        payload = self._payload()
        payload["participants"] = []
        response = self.client.post(self.bookings_url, payload, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_availability_cross_tenant_denied(self):
        other_tenant = f.make_tenant()
        other_template = f.make_template(other_tenant)
        day = self._future_start().date().isoformat()
        response = self.client.get(
            self.slots_url,
            {"event_template": str(other_template.id), "date_from": day, "date_to": day},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_available_slots_returns_data(self):
        day = self._future_start().date().isoformat()
        response = self.client.get(
            self.slots_url,
            {"event_template": str(self.template.id), "date_from": day, "date_to": day},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["status"])
        self.assertIsInstance(response.data["data"], list)
