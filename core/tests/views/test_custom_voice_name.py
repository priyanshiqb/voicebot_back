"""
Custom voice names: unique per tenant, ignoring case, and checkable before creating.

The name lives inside the JSON payload rather than in a column, so the rule reads the
payload's `name` key. It is the one rule on the payload; everything else is stored as sent.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.custom_voice import CustomVoice
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

URL = "/api/v1/core/custom-voices/"
CHECK = f"{URL}check-name/"


class CustomVoiceNameTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-voice-name")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-voice-name"
        )
        self.client_alex = self._client(self.tenant, "alex@example.com")
        self.client_bob = self._client(self.other_tenant, "bob@example.com")

    def _client(self, tenant, email):
        user = User.objects.create_user(username=email, email=email, password="password")
        TenantUserMapping.objects.create(user=user, tenant=tenant)
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _create(self, client=None, **payload):
        return (client or self.client_alex).post(URL, payload, format="json")

    def _is_unique(self, client=None, **payload):
        response = (client or self.client_alex).post(CHECK, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["data"]["is_unique"]

    # ------------------------------------------------------------- create

    def test_a_second_voice_with_the_same_name_is_refused(self):
        first = self._create(name="Riley — warm", language="en")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)

        response = self._create(name="riley — WARM", language="hi")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data["errors"])
        self.assertEqual(CustomVoice.objects.filter(tenant=self.tenant).count(), 1)

    def test_surrounding_spaces_do_not_make_a_different_name(self):
        self._create(name="Riley")

        self.assertEqual(
            self._create(name="  Riley ").status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_another_tenant_may_use_the_same_name(self):
        self._create(name="Riley")

        response = self._create(client=self.client_bob, name="Riley")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_deleted_voice_releases_its_name(self):
        voice_id = self._create(name="Riley").data["data"]["id"]
        CustomVoice.objects.filter(pk=voice_id).update(is_deleted=True)

        self.assertEqual(self._create(name="Riley").status_code, status.HTTP_201_CREATED)

    def test_a_payload_without_a_name_is_not_checked(self):
        self.assertEqual(self._create(language="en").status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._create(language="en").status_code, status.HTTP_201_CREATED)

    # ------------------------------------------------------------- check-name

    def test_check_name_reports_a_free_name(self):
        self.assertTrue(self._is_unique(name="Riley"))

    def test_check_name_reports_a_taken_name_in_any_casing(self):
        self._create(name="Riley")

        self.assertFalse(self._is_unique(name="RILEY"))

    def test_check_name_is_scoped_to_the_tenant(self):
        self._create(name="Riley")

        self.assertTrue(self._is_unique(client=self.client_bob, name="Riley"))

    def test_check_name_requires_a_name(self):
        response = self.client_alex.post(CHECK, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
