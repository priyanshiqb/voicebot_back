from datetime import date
from datetime import timezone as dt_timezone
from unittest import skipUnless
from unittest.mock import patch

from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from core.models.calendar_integration import CalendarIntegration
from core.services import availability, bookings as booking_service
from core.services.calendars.base import BusyInterval, OAuthTokens
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


class FakeProvider:
    def __init__(self, busy=None, push_id="ext-123"):
        self.busy = busy or []
        self.push_id = push_id
        self.pushed, self.updated, self.deleted = [], [], []

    def get_busy(self, integration, window_start, window_end):
        return list(self.busy)

    def push_event(self, integration, booking):
        self.pushed.append(booking.id)
        return self.push_id

    def update_event(self, integration, booking):
        self.updated.append(booking.id)

    def delete_event(self, integration, external_event_id):
        self.deleted.append(external_event_id)

    def refresh_access_token(self, integration):
        return integration

    def revoke(self, integration):
        pass


def make_integration(tenant, user):
    integration = CalendarIntegration(
        tenant=tenant,
        user=user,
        provider=CalendarIntegration.Provider.GOOGLE,
        account_email="host@gmail.com",
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    )
    integration.set_tokens(access_token="access", refresh_token="refresh")
    integration.save()
    return integration


@REQUIRES_PG
class CalendarTokenTests(TestCase):
    def test_token_encryption_roundtrip(self):
        tenant = f.make_tenant()
        user = f.make_user(tenant)
        integration = CalendarIntegration(
            tenant=tenant, user=user, provider=CalendarIntegration.Provider.GOOGLE
        )
        integration.set_tokens(access_token="secret-access", refresh_token="secret-refresh")
        integration.save()
        integration.refresh_from_db()

        # Stored value is ciphertext, not the plaintext token.
        self.assertNotIn("secret-access", integration.access_token)
        self.assertNotEqual(integration.access_token, "secret-access")
        self.assertEqual(integration.get_access_token(), "secret-access")
        self.assertEqual(integration.get_refresh_token(), "secret-refresh")


@REQUIRES_PG
class CalendarFreeBusyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = f.make_tenant()
        self.template = f.make_template(
            self.tenant, duration_minutes=30, slot_granularity_minutes=30
        )
        self.host = f.make_host(self.tenant, self.template)
        make_integration(self.tenant, self.host)

    @patch("core.services.calendars.get_provider_for")
    def test_external_busy_blocks_slot(self, mock_provider):
        mock_provider.return_value = FakeProvider(
            busy=[BusyInterval(at(10), at(10, 30))]
        )
        slots = availability.get_available_slots(
            self.tenant, self.template, date(2027, 3, 16), date(2027, 3, 16),
            target_host_id=self.host.id, now=NOW,
        )
        starts = {s.start for s in slots}
        self.assertNotIn(at(10), starts)
        self.assertIn(at(10, 30), starts)

    @patch("core.services.calendars.get_provider_for")
    def test_external_busy_excludes_host(self, mock_provider):
        mock_provider.return_value = FakeProvider(
            busy=[BusyInterval(at(10), at(10, 30))]
        )
        free = availability.eligible_free_hosts(
            self.tenant, self.template, at(10), at(10, 30), target_host_id=self.host.id
        )
        self.assertEqual(free, [])


@REQUIRES_PG
class CalendarSyncTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        make_integration(self.tenant, self.host)

    @patch("core.services.calendars.get_provider_for")
    def test_push_on_create_sets_external_id(self, mock_provider):
        fake = FakeProvider(push_id="evt-999")
        mock_provider.return_value = fake
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )
        booking.refresh_from_db()
        self.assertEqual(booking.external_event_id, "evt-999")
        self.assertIn(booking.id, fake.pushed)

    @patch("core.services.calendars.get_provider_for")
    def test_delete_on_cancel(self, mock_provider):
        fake = FakeProvider(push_id="evt-1")
        mock_provider.return_value = fake
        with self.captureOnCommitCallbacks(execute=True):
            booking = booking_service.create_booking(
                tenant=self.tenant,
                event_template=self.template,
                start_time=at(10),
                participants=ATTENDEES,
                target_host_id=self.host.id,
                now=NOW,
            )
        with self.captureOnCommitCallbacks(execute=True):
            booking_service.cancel_booking(booking=booking, enforce_lead=False, now=NOW)
        booking.refresh_from_db()
        self.assertIn("evt-1", fake.deleted)
        self.assertIsNone(booking.external_event_id)


@REQUIRES_PG
class CalendarOAuthApiTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.client = APIClient()

    @patch(
        "core.services.calendars.GoogleCalendarProvider.authorization_url",
        return_value="https://accounts.google.com/o/oauth2/auth?client_id=x",
    )
    def test_connect_returns_auth_url(self, _mock_url):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/core/calendar-integrations/connect/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            "accounts.google.com", response.data["data"]["authorization_url"]
        )

    @patch("core.services.calendars.GoogleCalendarProvider.exchange_code")
    def test_callback_creates_integration(self, mock_exchange):
        mock_exchange.return_value = OAuthTokens(
            access_token="a-token",
            refresh_token="r-token",
            token_expires_at=None,
            scope="calendar.events",
            account_email="host@gmail.com",
        )
        state = signing.dumps(
            {"user_id": str(self.user.id), "tenant_id": str(self.tenant.id)},
            salt="scheduling.calendar.oauth",
        )
        # Callback is unauthenticated (trusted via signed state).
        response = self.client.get(
            f"/api/v1/core/calendar-integrations/callback/?code=abc&state={state}"
        )
        self.assertIn(response.status_code, (200, 302))
        integration = CalendarIntegration.objects.get(
            tenant=self.tenant, user=self.user
        )
        self.assertEqual(integration.account_email, "host@gmail.com")
        self.assertEqual(integration.get_access_token(), "a-token")
        self.assertEqual(
            integration.sync_status, CalendarIntegration.SyncStatus.CONNECTED
        )

    def test_callback_rejects_bad_state(self):
        response = self.client.get(
            "/api/v1/core/calendar-integrations/callback/?code=abc&state=tampered"
        )
        self.assertEqual(response.status_code, 400)
