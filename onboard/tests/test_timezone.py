from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.timezone import Timezone
from onboard.models.temp_user import TempUser
from tenant.models.Tenant import Tenant
from user.models import CustomUser


class TimezoneTests(APITestCase):
    """
    Unit and integration tests for the Timezone model and TimezoneViewSet endpoints.
    """

    def setUp(self):
        # Create a test user and a superuser
        self.user = CustomUser.objects.create_user(
            email="testuser@example.com",
            password="testpassword123",
            first_name="Test",
            last_name="User"
        )
        self.superuser = CustomUser.objects.create_superuser(
            email="admin@example.com",
            password="adminpassword123",
            username="admin@example.com"
        )
        self.client.force_authenticate(user=self.superuser)

        # Create a default timezone for testing list/retrieve operations
        self.default_tz = Timezone.objects.create(
            offset="UTC+05:30",
            region="Mumbai, Delhi, Kolkata, Colombo",
            timezone="Asia/Kolkata",
            is_active=True
        )

        self.list_url = reverse("api-v1:timezone-list")
        self.dropdown_url = reverse("api-v1:timezone-dropdown")
        self.detail_url = reverse("api-v1:timezone-detail", kwargs={"pk": self.default_tz.id})

    def test_timezone_model_defaults(self):
        """
        Verify that Tenant, CustomUser, and TempUser fall back to UTC default if not provided.
        """
        # Create a UTC timezone (if not already seeded or existing)
        utc_tz, _ = Timezone.objects.get_or_create(
            timezone="UTC",
            defaults={"offset": "UTC+00:00", "region": "Coordinated Universal Time"}
        )

        # Create Tenant with no timezone
        tenant = Tenant.objects.create(
            name="Test Tenant",
            subdomain="test-subdomain"
        )
        self.assertEqual(tenant.timezone, utc_tz)

        # Create CustomUser with no timezone
        user = CustomUser.objects.create_user(
            email="newuser@example.com",
            password="newpassword123"
        )
        self.assertEqual(user.timezone, utc_tz)

        # Create TempUser with no timezone
        temp_user = TempUser.objects.create(
            email="temp@example.com",
            first_name="Temp",
            last_name="User",
            business_name="Temp Biz",
            password_hash="some-hash"
        )
        self.assertEqual(temp_user.timezone, utc_tz)

    def test_list_timezones_endpoint(self):
        """
        Test GET request to list timezones.
        """
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        
        # Ensure our created timezone is in the response results
        results = response.data["data"]
        self.assertTrue(any(item["timezone"] == "Asia/Kolkata" for item in results))

    def test_retrieve_timezone_endpoint(self):
        """
        Test GET request to retrieve timezone by ID.
        """
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["timezone"], "Asia/Kolkata")

    def test_create_timezone_endpoint(self):
        """
        Test POST request to create a timezone.
        """
        payload = {
            "offset": "UTC+09:00",
            "region": "Tokyo, Seoul, Pyongyang",
            "timezone": "Asia/Tokyo",
            "is_active": True
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])

        created_tz = Timezone.objects.get(timezone="Asia/Tokyo")
        self.assertEqual(created_tz.offset, "UTC+09:00")

    def test_soft_delete_timezone_endpoint(self):
        """
        Test DELETE request soft-deletes a timezone.
        """
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        # Soft deletion checks
        self.assertFalse(Timezone.objects.filter(id=self.default_tz.id).exists())
        self.assertTrue(Timezone.all_objects.filter(id=self.default_tz.id).exists())

    def test_dropdown_endpoint(self):
        """
        Test GET request to dropdown returns value/label structure.
        """
        response = self.client.get(self.dropdown_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        results = response.data["data"]
        # Find Asia/Kolkata in dropdown list
        dropdown_item = next(item for item in results if item["value"] == str(self.default_tz.id))
        self.assertEqual(dropdown_item["label"], "Asia/Kolkata (UTC+05:30)")

    def test_unauthenticated_access_endpoints(self):
        """
        Test that unauthenticated clients can access list, retrieve, and dropdown,
        but are blocked from mutating timezones.
        """
        self.client.force_authenticate(user=None)

        # 1. Test unauthenticated LIST
        list_response = self.client.get(self.list_url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)

        # 2. Test unauthenticated RETRIEVE
        detail_response = self.client.get(self.detail_url)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)

        # 3. Test unauthenticated DROPDOWN
        dropdown_response = self.client.get(self.dropdown_url)
        self.assertEqual(dropdown_response.status_code, status.HTTP_200_OK)

        # 4. Test unauthenticated MUTATION (create is blocked)
        payload = {
            "offset": "UTC+00:00",
            "region": "UTC",
            "timezone": "UTC"
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_standard_user_blocked_mutating(self):
        """
        Test that authenticated non-superuser standard clients are blocked from mutating timezones (403 Forbidden).
        """
        self.client.force_authenticate(user=self.user)

        # 1. Test POST creation is blocked
        payload = {
            "offset": "UTC+00:00",
            "region": "UTC",
            "timezone": "UTC"
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Test DELETE is blocked
        delete_response = self.client.delete(self.detail_url)
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
