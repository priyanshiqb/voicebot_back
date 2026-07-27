"""Microsoft 365 / Outlook (Graph) provider + wiring tests.

Mirrors the Google calendar suite: the provider's HTTP is faked (patch
`requests.request`), OAuth classmethods are patched at the view boundary, and the
webhook / watch-gating behaviour is exercised end to end. Run on Postgres:
    uv run python manage.py test core.tests.scheduling.test_calendar_microsoft \
        --settings=config.settings.test_postgres
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import skipUnless
from unittest.mock import patch

from django.core import signing
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models.calendar_integration import CalendarIntegration
from core.services import calendar_sync
from core.services.calendars.base import OAuthTokens
from core.services.calendars.microsoft import MicrosoftCalendarProvider, _parse_graph_dt
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import at
from core.tests.scheduling.test_webhook_sync import FakeSyncProvider

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

UTC = dt_timezone.utc
MS_WEBHOOK_URL = "/api/v1/core/calendar-webhook/microsoft/"


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, headers=None):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            err = requests.HTTPError(str(self.status_code))
            err.response = self
            raise err


class FakeBooking:
    def __init__(self, external_event_id=""):
        self.title = "Demo"
        self.start_time = datetime(2027, 3, 16, 10, 0, tzinfo=UTC)
        self.end_time = datetime(2027, 3, 16, 10, 30, tzinfo=UTC)
        self.external_event_id = external_event_id


def make_ms_integration(tenant, user):
    integration = CalendarIntegration(
        tenant=tenant,
        user=user,
        provider=CalendarIntegration.Provider.OUTLOOK,
        account_email="host@outlook.com",
        sync_status=CalendarIntegration.SyncStatus.CONNECTED,
    )
    integration.set_tokens(access_token="access", refresh_token="refresh")
    integration.save()
    return integration


class GraphDateParseTests(TestCase):
    def test_parse_seven_digit_fractional_as_utc(self):
        parsed = _parse_graph_dt(
            {"dateTime": "2027-03-16T10:00:00.0000000", "timeZone": "UTC"}
        )
        self.assertEqual(parsed, datetime(2027, 3, 16, 10, 0, tzinfo=UTC))

    def test_parse_trailing_z(self):
        parsed = _parse_graph_dt({"dateTime": "2027-03-16T10:00:00Z"})
        self.assertEqual(parsed, datetime(2027, 3, 16, 10, 0, tzinfo=UTC))

    def test_parse_empty_is_none(self):
        self.assertIsNone(_parse_graph_dt(None))
        self.assertIsNone(_parse_graph_dt({}))


@REQUIRES_PG
class MicrosoftProviderMappingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.integration = make_ms_integration(self.tenant, self.user)
        self.provider = MicrosoftCalendarProvider()

    def test_get_busy_maps_only_busy_statuses(self):
        payload = {
            "value": [
                {
                    "scheduleItems": [
                        {
                            "status": "busy",
                            "start": {"dateTime": "2027-03-16T10:00:00.0000000", "timeZone": "UTC"},
                            "end": {"dateTime": "2027-03-16T10:30:00.0000000", "timeZone": "UTC"},
                        },
                        {
                            "status": "free",
                            "start": {"dateTime": "2027-03-16T11:00:00.0000000", "timeZone": "UTC"},
                            "end": {"dateTime": "2027-03-16T11:30:00.0000000", "timeZone": "UTC"},
                        },
                        {
                            "status": "tentative",
                            "start": {"dateTime": "2027-03-16T12:00:00.0000000", "timeZone": "UTC"},
                            "end": {"dateTime": "2027-03-16T12:30:00.0000000", "timeZone": "UTC"},
                        },
                    ]
                }
            ]
        }
        with patch("requests.request", return_value=FakeResponse(payload)):
            intervals = self.provider.get_busy(self.integration, at(9), at(18))
        self.assertEqual(len(intervals), 2)  # busy + tentative, not free
        self.assertEqual(intervals[0].start, datetime(2027, 3, 16, 10, 0, tzinfo=UTC))
        self.assertEqual(intervals[0].end, datetime(2027, 3, 16, 10, 30, tzinfo=UTC))
        self.assertEqual(intervals[1].start, datetime(2027, 3, 16, 12, 0, tzinfo=UTC))

    def test_push_event_returns_external_id(self):
        with patch("requests.request", return_value=FakeResponse({"id": "evt-1"})):
            ext_id = self.provider.push_event(self.integration, FakeBooking())
        self.assertEqual(ext_id, "evt-1")

    def test_update_event_patches_by_id(self):
        with patch("requests.request", return_value=FakeResponse({})) as mock_req:
            self.provider.update_event(self.integration, FakeBooking(external_event_id="evt-42"))
        self.assertEqual(mock_req.call_args.args[0], "PATCH")
        self.assertTrue(mock_req.call_args.args[1].endswith("/me/events/evt-42"))

    def test_delete_event_swallows_404(self):
        with patch("requests.request", return_value=FakeResponse({}, status_code=404)):
            # Must not raise — 404 means already gone.
            self.provider.delete_event(self.integration, "evt-gone")

    def test_sync_events_maps_changes_and_stores_delta_link(self):
        payload = {
            "value": [
                {
                    "id": "ev-1",
                    "start": {"dateTime": "2027-03-16T10:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2027-03-16T11:00:00.0000000", "timeZone": "UTC"},
                },
                {"id": "ev-2", "@removed": {"reason": "deleted"}},
            ],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=NEXT",
        }
        with patch("requests.request", return_value=FakeResponse(payload)):
            result = self.provider.sync_events(self.integration, at(9), at(18))
        self.assertEqual(len(result.changes), 2)
        ev1, ev2 = result.changes
        self.assertFalse(ev1.cancelled)
        self.assertEqual(ev1.start, datetime(2027, 3, 16, 10, 0, tzinfo=UTC))
        self.assertEqual(ev1.end, datetime(2027, 3, 16, 11, 0, tzinfo=UTC))
        self.assertTrue(ev2.cancelled)
        self.assertEqual(ev2.external_id, "ev-2")
        self.assertEqual(result.sync_token, payload["@odata.deltaLink"])


@REQUIRES_PG
class MicrosoftOAuthApiTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.client = APIClient()

    @patch(
        "core.services.calendars.MicrosoftCalendarProvider.authorization_url",
        return_value="https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
    )
    def test_connect_outlook_returns_auth_url(self, _mock_url):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            "/api/v1/core/calendar-integrations/connect/?provider=OUTLOOK"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            "login.microsoftonline.com", response.data["data"]["authorization_url"]
        )

    def test_connect_rejects_unknown_provider(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            "/api/v1/core/calendar-integrations/connect/?provider=YAHOO"
        )
        self.assertEqual(response.status_code, 400)

    @patch("core.services.calendars.MicrosoftCalendarProvider.exchange_code")
    def test_callback_creates_outlook_integration(self, mock_exchange):
        mock_exchange.return_value = OAuthTokens(
            access_token="a-token",
            refresh_token="r-token",
            token_expires_at=None,
            scope="Calendars.ReadWrite",
            account_email="host@outlook.com",
        )
        state = signing.dumps(
            {
                "user_id": str(self.user.id),
                "tenant_id": str(self.tenant.id),
                "provider": "OUTLOOK",
            },
            salt="scheduling.calendar.oauth",
        )
        response = self.client.get(
            f"/api/v1/core/calendar-integrations/callback/?code=abc&state={state}"
        )
        self.assertIn(response.status_code, (200, 302))
        integration = CalendarIntegration.objects.get(
            tenant=self.tenant,
            user=self.user,
            provider=CalendarIntegration.Provider.OUTLOOK,
        )
        self.assertEqual(integration.account_email, "host@outlook.com")
        self.assertEqual(integration.get_access_token(), "a-token")
        self.assertEqual(
            integration.sync_status, CalendarIntegration.SyncStatus.CONNECTED
        )


@REQUIRES_PG
class MicrosoftWebhookTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.integration = make_ms_integration(self.tenant, self.user)
        self.integration.watch_channel_id = "sub-1"
        self.integration.watch_token = "secret"
        self.integration.save()
        self.client = APIClient()

    def test_validation_handshake_echoes_token(self):
        response = self.client.post(MS_WEBHOOK_URL + "?validationToken=abc123")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"abc123")
        self.assertTrue(response["Content-Type"].startswith("text/plain"))

    def _notify(self, subscription_id, client_state):
        return self.client.post(
            MS_WEBHOOK_URL,
            {
                "value": [
                    {
                        "subscriptionId": subscription_id,
                        "clientState": client_state,
                        "resourceData": {"id": "evt"},
                    }
                ]
            },
            format="json",
        )

    def test_valid_notification_triggers_sync(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._notify("sub-1", "secret")
            self.assertEqual(response.status_code, 202)
            mock_delay.assert_called_once_with(str(self.integration.id))

    def test_invalid_client_state_ignored(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._notify("sub-1", "wrong")
            self.assertEqual(response.status_code, 202)
            mock_delay.assert_not_called()

    def test_unknown_subscription_ignored(self):
        with patch("core.tasks.incremental_calendar_sync.delay") as mock_delay:
            response = self._notify("sub-unknown", "secret")
            self.assertEqual(response.status_code, 202)
            mock_delay.assert_not_called()


@REQUIRES_PG
class MicrosoftWatchGatingTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.integration = make_ms_integration(self.tenant, self.user)

    @override_settings(MICROSOFT_CALENDAR_WEBHOOK_URL="https://example.com/ms")
    @patch("core.services.calendars.get_provider_for")
    def test_setup_watch_uses_microsoft_url(self, mock_provider):
        fake = FakeSyncProvider()
        mock_provider.return_value = fake
        self.assertTrue(calendar_sync.setup_watch(self.integration))
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.watch_channel_id, "chan-1")
        self.assertTrue(self.integration.watch_token)
        self.assertEqual(fake.watch_setup, 1)

    @override_settings(
        GOOGLE_CALENDAR_WEBHOOK_URL="https://example.com/google",
        MICROSOFT_CALENDAR_WEBHOOK_URL="",
    )
    @patch("core.services.calendars.get_provider_for")
    def test_setup_watch_noop_when_only_google_configured(self, mock_provider):
        # An Outlook integration must NOT establish a watch off the Google URL.
        self.assertFalse(calendar_sync.setup_watch(self.integration))
        mock_provider.assert_not_called()
