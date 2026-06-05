from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.language import Language
from onboard.models.temp_user import TempUser
from tenant.models.Tenant import Tenant
from user.models import CustomUser


class LanguageTests(APITestCase):
    """
    Unit and integration tests for the Language model and LanguageViewSet endpoints.
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

        # Retrieve or create a default language for testing list/retrieve operations
        self.default_lang, _ = Language.objects.get_or_create(
            code="es-ES",
            defaults={"name": "Spanish (Spain)", "is_active": True}
        )

        self.list_url = reverse("api-v1:language-list")
        self.dropdown_url = reverse("api-v1:language-dropdown")
        self.detail_url = reverse("api-v1:language-detail", kwargs={"pk": self.default_lang.id})

    def test_language_model_defaults(self):
        """
        Verify that Tenant, CustomUser, and TempUser fall back to en-IN default if not provided.
        """
        # Create a en-IN language (if not already seeded or existing)
        enin_lang, _ = Language.objects.get_or_create(
            code="en-IN",
            defaults={"name": "English (India)"}
        )

        # Create Tenant with no language
        tenant = Tenant.objects.create(
            name="Test Tenant",
            subdomain="test-subdomain-lang"
        )
        self.assertEqual(tenant.language, enin_lang)

        # Create CustomUser with no language
        user = CustomUser.objects.create_user(
            email="newuserlang@example.com",
            password="newpassword123"
        )
        self.assertEqual(user.language, enin_lang)

        # Create TempUser with no language
        temp_user = TempUser.objects.create(
            email="templang@example.com",
            first_name="Temp",
            last_name="User",
            business_name="Temp Biz",
            password_hash="some-hash"
        )
        self.assertEqual(temp_user.language, enin_lang)

    def test_list_languages_endpoint(self):
        """
        Test GET request to list languages.
        """
        response = self.client.get(f"{self.list_url}?page_size=100")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        
        # Ensure our created language is in the response results
        results = response.data["data"]
        self.assertTrue(any(item["code"] == "es-ES" for item in results))

    def test_retrieve_language_endpoint(self):
        """
        Test GET request to retrieve language by ID.
        """
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["code"], "es-ES")

    def test_create_language_endpoint(self):
        """
        Test POST request to create a language.
        """
        payload = {
            "name": "Test Language",
            "code": "tl-TL",
            "is_active": True
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])

        created_lang = Language.objects.get(code="tl-TL")
        self.assertEqual(created_lang.name, "Test Language")

    def test_soft_delete_language_endpoint(self):
        """
        Test DELETE request soft-deletes a language.
        """
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        # Soft deletion checks
        self.assertFalse(Language.objects.filter(id=self.default_lang.id).exists())
        self.assertTrue(Language.all_objects.filter(id=self.default_lang.id).exists())

    def test_dropdown_endpoint(self):
        """
        Test GET request to dropdown returns value/label structure.
        """
        response = self.client.get(f"{self.dropdown_url}?page_size=100")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        results = response.data["data"]
        # Find Spanish (Spain) in dropdown list
        dropdown_item = next(item for item in results if item["value"] == str(self.default_lang.id))
        self.assertEqual(dropdown_item["label"], "Spanish (Spain) (es-ES)")

    def test_unauthenticated_access_endpoints(self):
        """
        Test that unauthenticated clients are blocked from accessing any endpoints (401 Unauthorized).
        """
        self.client.force_authenticate(user=None)

        # 1. Test unauthenticated LIST (blocked)
        list_response = self.client.get(self.list_url)
        self.assertEqual(list_response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 2. Test unauthenticated RETRIEVE (blocked)
        detail_response = self.client.get(self.detail_url)
        self.assertEqual(detail_response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 3. Test unauthenticated DROPDOWN (blocked)
        dropdown_response = self.client.get(self.dropdown_url)
        self.assertEqual(dropdown_response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 4. Test unauthenticated MUTATION (create is blocked)
        payload = {
            "name": "Test Language",
            "code": "tl-TL",
            "is_active": True
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_standard_user_blocked_mutating(self):
        """
        Test that authenticated non-superuser standard clients can read languages,
        but are blocked from mutating languages (403 Forbidden).
        """
        self.client.force_authenticate(user=self.user)

        # 1. Test GET list is allowed
        list_response = self.client.get(self.list_url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)

        # 2. Test GET retrieve is allowed
        detail_response = self.client.get(self.detail_url)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)

        # 3. Test GET dropdown is allowed
        dropdown_response = self.client.get(self.dropdown_url)
        self.assertEqual(dropdown_response.status_code, status.HTTP_200_OK)

        # 4. Test POST creation is blocked
        payload = {
            "name": "Test Language",
            "code": "tl-TL",
            "is_active": True
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

        # 5. Test DELETE is blocked
        delete_response = self.client.delete(self.detail_url)
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
