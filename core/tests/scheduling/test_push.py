from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models.device_token import DeviceToken
from core.services import bookings as booking_service, push, reschedule
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)


@REQUIRES_PG
class DeviceTokenApiTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/v1/core/device-tokens/"

    def test_register_is_upsert(self):
        response = self.client.post(
            self.url, {"token": "abc", "platform": "WEB"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        device = DeviceToken.objects.get(tenant=self.tenant, token="abc")
        self.assertEqual(device.user_id, self.user.id)

        device.is_active = False
        device.save()
        response = self.client.post(
            self.url, {"token": "abc", "platform": "IOS"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        device.refresh_from_db()
        self.assertTrue(device.is_active)
        self.assertEqual(device.platform, "IOS")
        self.assertEqual(DeviceToken.objects.filter(token="abc").count(), 1)

    def test_deactivate(self):
        self.client.post(self.url, {"token": "abc"}, format="json")
        response = self.client.post(
            self.url + "deactivate/", {"token": "abc"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeviceToken.objects.get(token="abc").is_active)


class _Result:
    def __init__(self, success=True, exception=None):
        self.success = success
        self.exception = exception


class _Batch:
    def __init__(self, *results):
        self.responses = list(results)


@REQUIRES_PG
class PushSendTests(TestCase):
    """Delivery goes through one multicast call, not a loop of single sends.

    The mocks target ``send_each_for_multicast`` and hand failures back *inside* the
    batch response, which is how firebase-admin actually reports per-token outcomes
    — an exception raised from the call itself means the whole batch failed and
    nothing is known about any individual token.
    """

    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)

    @patch("core.services.push._ensure_app", return_value=True)
    @patch("firebase_admin.messaging.send_each_for_multicast")
    def test_send_to_user(self, mock_send, _ensure):
        DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-1", platform="WEB"
        )
        mock_send.return_value = _Batch(_Result())
        sent = push.send_to_user(self.tenant, self.user, "Title", "Body")
        self.assertEqual(sent, 1)
        mock_send.assert_called_once()

    @patch("core.services.push._ensure_app", return_value=True)
    @patch("firebase_admin.messaging.send_each_for_multicast")
    def test_invalid_token_deactivated(self, mock_send, _ensure):
        class UnregisteredError(Exception):
            pass

        mock_send.return_value = _Batch(_Result(False, UnregisteredError("gone")))
        device = DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-x", platform="WEB"
        )
        push.send_to_user(self.tenant, self.user, "t", "b")
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_send_noop_without_config(self):
        """An unset credential disables push quietly.

        The setting is overridden rather than assumed empty: a developer whose real
        `.env` sets FIREBASE_CREDENTIALS would otherwise have this test try to load
        their actual credentials. `_app_ready` is a module global, so it is reset
        and restored too — without that, whether this test exercises `_ensure_app`
        at all depends on which tests ran before it.
        """
        DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-2", platform="WEB"
        )
        previous = push._app_ready
        push._app_ready = False
        self.addCleanup(setattr, push, "_app_ready", previous)

        with override_settings(FIREBASE_CREDENTIALS=""):
            self.assertEqual(push.send_to_user(self.tenant, self.user, "t", "b"), 0)

    def test_a_credential_that_cannot_be_loaded_is_reported_not_raised(self):
        """A broken credential must not take down every send.

        Before this, `_ensure_app` let `Certificate()` raise straight through
        `_send` into the caller — a Celery task, or a request. It is now caught,
        recorded, and surfaced by the diagnostic endpoint, so "push is on but
        nothing arrives" has an answer instead of a stack trace.
        """
        previous = push._app_ready
        push._app_ready = False
        self.addCleanup(setattr, push, "_app_ready", previous)
        self.addCleanup(setattr, push, "_init_error", None)

        with override_settings(FIREBASE_CREDENTIALS="/nonexistent/creds.json"):
            self.assertEqual(push.send_to_user(self.tenant, self.user, "t", "b"), 0)
            self.assertIsNotNone(push.configuration_error())

    def test_inline_json_credentials_are_accepted(self):
        """Pasting the service-account JSON into the env var is the obvious mistake.

        It is accepted rather than rejected — plenty of platforms only offer
        environment variables — but it must be recognised as JSON rather than
        handed to `Certificate()` as if it were a filename.
        """
        from unittest.mock import MagicMock

        creds = MagicMock()
        push._build_credential(creds, '  {"type": "service_account"}  ')
        creds.Certificate.assert_called_once_with({"type": "service_account"})

        creds.reset_mock()
        push._build_credential(creds, "/run/secrets/firebase.json")
        creds.Certificate.assert_called_once_with("/run/secrets/firebase.json")


@REQUIRES_PG
class ReschedulePushTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.template = f.make_template(self.tenant)

    @patch("core.services.push.notify_tenant_admins")
    def test_requires_reschedule_pushes_admins(self, mock_push):
        host1 = f.make_host(self.tenant, self.template)  # only host -> flag
        booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=at(10),
            participants=ATTENDEES,
            target_host_id=host1.id,
            now=NOW,
        )
        leave = f.make_exception(self.tenant, host1, at(9), at(12))
        with self.captureOnCommitCallbacks(execute=True):
            reschedule.handle_leave(self.tenant, leave.id, now=NOW)
        self.assertTrue(mock_push.called)
