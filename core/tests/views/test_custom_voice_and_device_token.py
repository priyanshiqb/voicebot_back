"""
Regression tests for two endpoints that previously had no API coverage.

Custom voices returned only the stored JSON blob, so the row's primary key never
appeared in any response — and retrieve/delete route on that key, which made every
record created through the API unreachable through the API.

Device token registration read-then-wrote against a partial unique index, so two
simultaneous registrations of the same token raced and the loser surfaced as a 500.
Clients are told to POST on every app start, which is exactly how that happens.
"""

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.custom_voice import CustomVoice
from core.models.device_token import DeviceToken
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User


class CustomVoiceViewSetTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-voice")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-voice"
        )

        self.user = User.objects.create_user(
            username="voice-alex", email="voice-alex@example.com", password="password"
        )
        self.other_user = User.objects.create_user(
            username="voice-bob", email="voice-bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

        self.payload = {
            "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
            "name": "Riley - warm",
            "language": "en",
            "record_type": "upload",
        }

    def test_create_returns_the_row_id_and_that_id_addresses_the_record(self):
        """The whole point: what create returns must work in the detail URL."""
        response = self.client_alex.post(
            "/api/v1/core/custom-voices/", self.payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        returned_id = response.data["data"]["id"]
        self.assertEqual(returned_id, str(CustomVoice.objects.get().id))

        # The payload still round-trips untouched alongside it.
        self.assertEqual(response.data["data"]["name"], "Riley - warm")
        self.assertEqual(response.data["data"]["record_type"], "upload")
        self.assertIsNotNone(response.data["data"]["created_at"])

        retrieve = self.client_alex.get(f"/api/v1/core/custom-voices/{returned_id}/")
        self.assertEqual(retrieve.status_code, status.HTTP_200_OK)
        self.assertEqual(retrieve.data["data"]["id"], returned_id)

        delete = self.client_alex.delete(f"/api/v1/core/custom-voices/{returned_id}/")
        self.assertEqual(delete.status_code, status.HTTP_200_OK)
        self.assertTrue(CustomVoice.all_objects.get(id=returned_id).is_deleted)

    def test_a_payload_id_does_not_shadow_the_primary_key(self):
        """
        A client-supplied `id` is inert — it never routes — so it must not win over
        the real key, or the record goes back to being unaddressable.
        """
        response = self.client_alex.post(
            "/api/v1/core/custom-voices/",
            {**self.payload, "id": "provider-side-identifier"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        row = CustomVoice.objects.get()
        self.assertEqual(response.data["data"]["id"], str(row.id))
        # Stored verbatim: the provider's value is still in the blob on disk.
        self.assertEqual(row.data["id"], "provider-side-identifier")

    def test_list_entries_carry_ids(self):
        self.client_alex.post(
            "/api/v1/core/custom-voices/", self.payload, format="json"
        )
        response = self.client_alex.get("/api/v1/core/custom-voices/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(
            response.data["data"][0]["id"], str(CustomVoice.objects.get().id)
        )

    def test_another_tenants_record_is_404_not_403(self):
        created = self.client_alex.post(
            "/api/v1/core/custom-voices/", self.payload, format="json"
        )
        voice_id = created.data["data"]["id"]

        response = self.client_bob.get(f"/api/v1/core/custom-voices/{voice_id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client_bob.get("/api/v1/core/custom-voices/").data["data"], [])


class DeviceTokenRegistrationTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-device")
        self.user = User.objects.create_user(
            username="device-alex", email="device-alex@example.com", password="password"
        )
        self.second_user = User.objects.create_user(
            username="device-carol",
            email="device-carol@example.com",
            password="password",
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.second_user, tenant=self.tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_carol = APIClient()
        self.client_carol.force_authenticate(user=self.second_user)

    def test_re_registering_updates_in_place_rather_than_duplicating(self):
        first = self.client_alex.post(
            "/api/v1/core/device-tokens/",
            {"token": "fcm-abc", "platform": "WEB"},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client_alex.post(
            "/api/v1/core/device-tokens/",
            {"token": "fcm-abc", "platform": "IOS"},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data["data"]["id"], first.data["data"]["id"])
        self.assertEqual(second.data["data"]["platform"], "IOS")
        self.assertEqual(DeviceToken.objects.filter(token="fcm-abc").count(), 1)

    def test_registering_reactivates_a_token_firebase_had_disowned(self):
        created = self.client_alex.post(
            "/api/v1/core/device-tokens/", {"token": "fcm-stale"}, format="json"
        )
        DeviceToken.objects.filter(token="fcm-stale").update(is_active=False)

        response = self.client_alex.post(
            "/api/v1/core/device-tokens/", {"token": "fcm-stale"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["id"], created.data["data"]["id"])
        self.assertTrue(response.data["data"]["is_active"])

    def test_a_lost_insert_race_recovers_instead_of_500ing(self):
        """
        Simulates the loser of a concurrent registration: the row is invisible when the
        view looks for it, but exists by the time the view inserts. Before the fix this
        was an unhandled IntegrityError and a 500.

        The competing row is created *before* the request rather than from inside the
        mocked insert. The view wraps its insert in a savepoint, so a row written in
        there would be rolled back along with the failure and there would be nothing
        left to recover — which is not what a real concurrent commit looks like.
        """
        DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.second_user,
            token="fcm-race",
            platform="ANDROID",
        )

        original_filter = DeviceToken.objects.filter
        calls = {"n": 0}

        def blind_on_first_look(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return DeviceToken.objects.none()
            return original_filter(*args, **kwargs)

        DeviceToken.objects.filter = blind_on_first_look
        try:
            response = self.client_alex.post(
                "/api/v1/core/device-tokens/",
                {"token": "fcm-race", "platform": "WEB"},
                format="json",
            )
        finally:
            DeviceToken.objects.filter = original_filter

        # 1 blind lookup + 1 recovery lookup + 1 from the per-user cap check that
        # runs after every registration. Asserted exactly rather than `>= 2` so a
        # future change that adds a query here has to say so out loud.
        self.assertEqual(calls["n"], 3, "the recovery lookup should have run")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DeviceToken.objects.filter(token="fcm-race").count(), 1)

        # Recovery takes the update branch, so the caller's registration still applies.
        recovered = DeviceToken.objects.get(token="fcm-race")
        self.assertEqual(recovered.user_id, self.user.id)
        self.assertEqual(recovered.platform, "WEB")

    def test_re_registering_someone_elses_token_transfers_ownership(self):
        first = self.client_alex.post(
            "/api/v1/core/device-tokens/", {"token": "fcm-shared"}, format="json"
        )

        response = self.client_carol.post(
            "/api/v1/core/device-tokens/", {"token": "fcm-shared"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["id"], first.data["data"]["id"])
        self.assertEqual(response.data["data"]["user"], self.second_user.id)

        # Alex no longer sees it; Carol does.
        self.assertEqual(
            self.client_alex.get("/api/v1/core/device-tokens/").data["data"], []
        )
        self.assertEqual(
            len(self.client_carol.get("/api/v1/core/device-tokens/").data["data"]), 1
        )

    def test_deactivate_is_scoped_to_the_caller(self):
        self.client_alex.post(
            "/api/v1/core/device-tokens/", {"token": "fcm-alex"}, format="json"
        )

        # Carol naming Alex's token must not switch it off.
        response = self.client_carol.post(
            "/api/v1/core/device-tokens/deactivate/",
            {"token": "fcm-alex"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(DeviceToken.objects.get(token="fcm-alex").is_active)

        self.client_alex.post(
            "/api/v1/core/device-tokens/deactivate/",
            {"token": "fcm-alex"},
            format="json",
        )
        self.assertFalse(DeviceToken.objects.get(token="fcm-alex").is_active)

    def test_deactivate_requires_a_token(self):
        response = self.client_alex.post(
            "/api/v1/core/device-tokens/deactivate/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["message"], "token is required")
