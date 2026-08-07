"""
`Booking.source` is provenance, and it is derived from the authenticated caller.

Before this, `POST /core/bookings/` hardcoded `INTERNAL` for everyone. A VB_AGENT
token — the voicebot, booking over REST rather than through the in-process bridge
— produced bookings indistinguishable from ones a human made in the dashboard,
which is exactly the distinction the column exists to record.

It is deliberately NOT read from the request body. A value the caller asserts
about itself is not provenance: any human JWT could post `source: "AI"` and
inflate "the agent booked X% of appointments", with nothing to catch it. The
caller is already identified by its token.
"""

from datetime import timedelta
from unittest import skipUnless

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.booking import Booking
from core.tests.scheduling import factories as f
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken

BOOKINGS_URL = "/api/v1/core/bookings/"


@skipUnless(connection.vendor == "postgresql", "Scheduling tests require PostgreSQL.")
class BookingSourceTests(APITestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)

        self.human = APIClient()
        self.human.force_authenticate(user=self.actor)

        runner = User.objects.create_user(
            username="source_vb",
            email="source-vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        secret, prefix = "source_secret", "vb_source"
        AgentToken.objects.create(
            user=runner,
            name="Source",
            prefix=prefix,
            key=make_password(secret),
            never_expires=True,
        )
        self.agent = APIClient()
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {prefix}_{secret}")
        self.tenant_header = {"HTTP_X_TENANT_ID": str(self.tenant.id)}

    def _payload(self, hours=0):
        start = (timezone.now() + timedelta(days=2, hours=hours)).replace(
            minute=0, second=0, microsecond=0
        )
        return {
            "event_template": str(self.template.id),
            "start_time": start.isoformat(),
            "target_host": str(self.host.id),
            "participants": [{"name": "Cust", "email": "cust@example.com"}],
        }

    def _booking(self, response):
        return Booking.objects.get(pk=response.data["data"]["id"])

    # --- create ---------------------------------------------------------------

    def test_an_agent_token_books_as_ai(self):
        response = self.agent.post(
            BOOKINGS_URL, self._payload(), format="json", **self.tenant_header
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.AI)
        self.assertEqual(response.data["data"]["source"], "AI")

    def test_a_human_still_books_as_internal(self):
        response = self.human.post(BOOKINGS_URL, self._payload(1), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.INTERNAL)

    def test_a_human_cannot_claim_to_be_the_agent(self):
        """The reason this is derived rather than accepted: `source` is only worth
        reporting on if a caller cannot assert it about itself."""
        response = self.human.post(
            BOOKINGS_URL, {**self._payload(2), "source": "AI"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.INTERNAL)

    def test_an_agent_sending_a_contradictory_source_is_still_ai(self):
        response = self.agent.post(
            BOOKINGS_URL,
            {**self._payload(3), "source": "CUSTOMER"},
            format="json",
            **self.tenant_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.AI)

    # --- hold -----------------------------------------------------------------

    def test_an_agent_hold_is_tagged_ai(self):
        """A hold becomes a booking. If the two disagreed about provenance, the
        confirm step would silently launder an AI booking into a customer one."""
        response = self.agent.post(
            f"{BOOKINGS_URL}hold/",
            self._payload(4),
            format="json",
            **self.tenant_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.AI)

    def test_a_human_hold_keeps_the_customer_default(self):
        """Unchanged behaviour — a hold is normally someone picking a slot."""
        response = self.human.post(
            f"{BOOKINGS_URL}hold/", self._payload(5), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self._booking(response).source, Booking.Source.CUSTOMER)

    def test_provenance_survives_confirming_a_held_slot(self):
        held = self.agent.post(
            f"{BOOKINGS_URL}hold/",
            self._payload(6),
            format="json",
            **self.tenant_header,
        )
        booking_id = held.data["data"]["id"]

        confirmed = self.agent.post(
            f"{BOOKINGS_URL}{booking_id}/confirm/",
            {},
            format="json",
            **self.tenant_header,
        )

        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.data)
        booking = Booking.objects.get(pk=booking_id)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertEqual(booking.source, Booking.Source.AI)
