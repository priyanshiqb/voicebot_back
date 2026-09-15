"""
A recording the SIP provider hosts, attached to a call log as a link.

The alternative to the upload flow: when the provider already keeps the audio, the
runner sends `external_recording_url` in the create payload rather than copying the
file into blob storage. The link is shown to every user in the tenant, so what it may
contain is the thing worth pinning down.
"""

from django.contrib.auth.hashers import make_password
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken
from user.models.user_session import UserSession

URL = "/api/v1/core/agent-call-logs/"
PROVIDER_LINK = "https://recordings.provider.example/v1/calls/CA123/rec.mp3?token=abc"


class ExternalRecordingUrlTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-ext-rec")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )

        vb_user = User.objects.create_user(
            username="vb_ext_rec",
            email="vb-ext-rec@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        AgentToken.objects.create(
            user=vb_user,
            name="Runner",
            prefix="vb_extrec",
            key=make_password("secret"),
            never_expires=True,
        )
        self.client_vb = APIClient()
        self.client_vb.credentials(
            HTTP_AUTHORIZATION="Bearer vb_extrec_secret",
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )

        # The people who play the recording read it with a JWT, not an agent token.
        user = User.objects.create_user(
            username="viewer",
            email="viewer@example.com",
            password="password",
            user_type=User.UserType.USER,
        )
        TenantUserMapping.objects.create(user=user, tenant=self.tenant)
        access = RefreshToken.for_user(user).access_token
        UserSession.objects.create(
            user=user,
            access_jti=access.get("jti"),
            refresh_jti="dummy_refresh_jti",
            device_name="Test Client",
            ip_address="127.0.0.1",
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        self.client_user = APIClient()
        self.client_user.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _create(self, **fields):
        payload = {
            "provider_call_id": "call-ext-1",
            "agent": str(self.agent.id),
            "caller_number": "+14155557001",
            "direction": "inbound",
            **fields,
        }
        return self.client_vb.post(URL, payload, format="json")

    def test_link_is_stored_and_shown_to_tenant_users(self):
        response = self._create(external_recording_url=PROVIDER_LINK)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = AgentCallLog.objects.get(provider_call_id="call-ext-1")
        self.assertEqual(log.external_recording_url, PROVIDER_LINK)

        detail = self.client_user.get(f"{URL}{log.id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["data"]["external_recording_url"], PROVIDER_LINK)

    def test_omitting_it_still_creates_the_log(self):
        """Runners that do not send it yet must keep working."""
        response = self._create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["data"]["external_recording_url"])

    def test_blank_is_stored_as_null(self):
        response = self._create(external_recording_url="")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = AgentCallLog.objects.get(provider_call_id="call-ext-1")
        self.assertIsNone(log.external_recording_url)

    def test_only_http_and_https_are_accepted(self):
        for link in (
            "ftp://recordings.provider.example/rec.mp3",
            "javascript:alert(1)",
            "not a url",
        ):
            with self.subTest(link=link):
                response = self._create(external_recording_url=link)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("external_recording_url", response.data["errors"])
                self.assertFalse(AgentCallLog.objects.exists())

    def test_link_with_credentials_is_refused(self):
        """It is returned to every user in the tenant, so the login would leak."""
        response = self._create(
            external_recording_url="https://acct:s3cret@api.provider.example/rec.mp3"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("external_recording_url", response.data["errors"])
        self.assertFalse(AgentCallLog.objects.exists())

    def test_link_longer_than_the_column_is_refused(self):
        response = self._create(
            external_recording_url="https://provider.example/" + "a" * 2000
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(AgentCallLog.objects.exists())
