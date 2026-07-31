from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
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


@REQUIRES_PG
class PushSendTests(TestCase):
    def setUp(self):
        self.tenant = f.make_tenant()
        self.user = f.make_user(self.tenant)

    @patch("core.services.push._ensure_app", return_value=True)
    @patch("firebase_admin.messaging.send")
    def test_send_to_user(self, mock_send, _ensure):
        DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-1", platform="WEB"
        )
        sent = push.send_to_user(self.tenant, self.user, "Title", "Body")
        self.assertEqual(sent, 1)
        mock_send.assert_called_once()

    @patch("core.services.push._ensure_app", return_value=True)
    @patch("firebase_admin.messaging.send")
    def test_invalid_token_deactivated(self, mock_send, _ensure):
        class UnregisteredError(Exception):
            pass

        mock_send.side_effect = UnregisteredError("gone")
        device = DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-x", platform="WEB"
        )
        push.send_to_user(self.tenant, self.user, "t", "b")
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_send_noop_without_config(self):
        # FIREBASE_CREDENTIALS is empty in tests -> _ensure_app False -> 0 sent.
        DeviceToken.objects.create(
            tenant=self.tenant, user=self.user, token="tok-2", platform="WEB"
        )
        self.assertEqual(push.send_to_user(self.tenant, self.user, "t", "b"), 0)


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
