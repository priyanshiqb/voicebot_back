"""
BUG-001227 — connecting Google Calendar.

The callback is registered twice, at `…/callback/` (the router) and `…/callback`
(an explicit route, so a slash-less `GOOGLE_CALENDAR_REDIRECT_URI` does not 500 under
APPEND_SLASH). The auth bypass only recognised the first: it tested
`"callback" in action_map.values()`, which is an equality check against each value, and
the slash-less route's value is `"callback_slashless"`. Google's redirect therefore hit
a view still demanding an Authorization header and got a 401 — in every deployment
whose redirect URI has no trailing slash, which is how dev is configured.
"""

from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from core.models.calendar_integration import CalendarIntegration
from core.services.calendars.base import OAuthTokens
from core.tests.scheduling import factories as f

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

WITH_SLASH = "/api/v1/core/calendar-integrations/callback/"
WITHOUT_SLASH = "/api/v1/core/calendar-integrations/callback"


def _tokens():
    return OAuthTokens(
        access_token="a-token",
        refresh_token="r-token",
        token_expires_at=None,
        scope="calendar.events",
        account_email="host@gmail.com",
    )


@REQUIRES_PG
class CallbackRouteTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        # Anonymous on purpose: this is Google's browser redirect, which carries no
        # Authorization header. Authenticating here would hide the bug.
        self.client = APIClient()

    @patch("core.services.calendars.GoogleCalendarProvider.exchange_code")
    def test_the_slashless_route_is_not_behind_authentication(self, mock_exchange):
        mock_exchange.return_value = _tokens()
        state = f.oauth_state(self.user, self.tenant)

        response = self.client.get(f"{WITHOUT_SLASH}?code=abc&state={state}")

        self.assertNotEqual(response.status_code, 401)
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(
            CalendarIntegration.objects.filter(
                tenant=self.tenant, user=self.user
            ).exists()
        )

    @patch("core.services.calendars.GoogleCalendarProvider.exchange_code")
    def test_both_spellings_connect_the_same_integration(self, mock_exchange):
        mock_exchange.return_value = _tokens()

        # Not reset between the two: one integration per (tenant, user), so the second
        # run must land on the same row rather than making a duplicate.
        for path in (WITH_SLASH, WITHOUT_SLASH):
            with self.subTest(path=path):
                state = f.oauth_state(self.user, self.tenant)
                response = self.client.get(f"{path}?code=abc&state={state}")
                self.assertIn(response.status_code, (200, 302))
                integration = CalendarIntegration.objects.get(
                    tenant=self.tenant, user=self.user
                )
                self.assertEqual(
                    integration.sync_status,
                    CalendarIntegration.SyncStatus.CONNECTED,
                )

    def test_the_slashless_route_still_validates_its_state(self):
        """Skipping authentication must not mean skipping the signature check."""
        response = self.client.get(f"{WITHOUT_SLASH}?code=abc&state=tampered")

        # 302 when a frontend is configured, 400 when not — either way, refused.
        self.assertIn(response.status_code, (302, 400))
        if response.status_code == 302:
            self.assertIn("reason=invalid_state", response["Location"])
        self.assertFalse(CalendarIntegration.objects.exists())


@REQUIRES_PG
@override_settings(FRONTEND_PROJECT_URL="https://app.example.com")
class CallbackFailureRedirectTests(TestCase):
    """
    A failed exchange happens mid-redirect, in the host's browser. Returning the JSON
    envelope drops them on a page of raw API output with no way back.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.client = APIClient()

    def test_success_redirects_to_the_frontend_with_a_status(self):
        with patch(
            "core.services.calendars.GoogleCalendarProvider.exchange_code",
            return_value=_tokens(),
        ):
            state = f.oauth_state(self.user, self.tenant)
            response = self.client.get(f"{WITH_SLASH}?code=abc&state={state}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar_status=connected", response["Location"])

    def test_a_declined_consent_screen_redirects_with_a_reason(self):
        response = self.client.get(f"{WITH_SLASH}?error=access_denied")
        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar_status=error", response["Location"])
        self.assertIn("reason=consent_denied", response["Location"])

    def test_a_failed_exchange_redirects_with_a_reason(self):
        with patch(
            "core.services.calendars.GoogleCalendarProvider.exchange_code",
            side_effect=RuntimeError("boom"),
        ):
            state = f.oauth_state(self.user, self.tenant)
            response = self.client.get(f"{WITH_SLASH}?code=abc&state={state}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("reason=exchange_failed", response["Location"])

    @override_settings(FRONTEND_PROJECT_URL="")
    def test_without_a_frontend_the_failure_is_still_a_json_error(self):
        response = self.client.get(f"{WITH_SLASH}?error=access_denied")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"]["code"], "consent_denied")


class RelaxedScopeTests(SimpleTestCase):
    """
    Google returns the scopes actually granted, which with `include_granted_scopes=true`
    is a superset of what we asked for. oauthlib treats any difference as a fatal
    `Warning` out of `fetch_token` unless this switch is set, so the connect flow would
    fail for exactly the hosts who have granted this client something before — and pass
    in testing with a fresh account.
    """

    def test_exchange_relaxes_the_scope_check(self):
        import os

        from core.services.calendars.google import GoogleCalendarProvider

        os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)

        captured = {}

        class FakeCredentials:
            token = "a"
            refresh_token = "r"
            expiry = None
            scopes = ["calendar.events"]

        class FakeFlow:
            credentials = FakeCredentials()

            def fetch_token(self, **kwargs):
                captured["relaxed"] = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")

        with patch.object(GoogleCalendarProvider, "_flow", return_value=FakeFlow()), \
             patch.object(GoogleCalendarProvider, "_account_email", return_value=""):
            GoogleCalendarProvider.exchange_code("code")

        self.assertEqual(captured["relaxed"], "1")
