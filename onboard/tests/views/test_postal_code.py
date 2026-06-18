from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.postal_code import PostalCode
from user.models import CustomUser


class PostalCodeViewTests(APITestCase):
    """
    Integration tests for the PostalCodeViewSet endpoints.
    """

    def setUp(self):
        # Create test users
        self.user = CustomUser.objects.create_user(
            email="regularuser@example.com",
            password="userpassword123",
            first_name="Regular",
            last_name="User"
        )
        self.superuser = CustomUser.objects.create_superuser(
            email="adminuser@example.com",
            password="adminpassword123",
            username="adminuser@example.com"
        )
        # Authenticate as superuser by default
        self.client.force_authenticate(user=self.superuser)

        # Create two test postal codes (one in Dubai, one in Abu Dhabi)
        self.postal_dubai = PostalCode.objects.create(
            country_code="AE",
            postal_code="32867 98458",
            place_name="Al Wuheida",
            admin_name1="Dubai",
            admin_code1="03",
            latitude=25.293300,
            longitude=55.339900,
            accuracy=6
        )
        self.postal_auh = PostalCode.objects.create(
            country_code="AE",
            postal_code="21221 11212",
            place_name="Al Khalidiya",
            admin_name1="Abu Dhabi",
            admin_code1="01",
            latitude=24.468600,
            longitude=54.341300,
            accuracy=6
        )

        self.list_url = reverse("api-v1:postal-code-list")
        self.dropdown_url = reverse("api-v1:postal-code-dropdown")
        self.nearest_url = f"{self.list_url}nearest/"
        self.detail_url = reverse("api-v1:postal-code-detail", kwargs={"pk": self.postal_dubai.id})

    def test_list_postal_codes(self):
        """
        Verify that listing postal codes works and supports query parameters.
        """
        # Test standard list
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 2)

        # Test search filter (Dubai)
        response = self.client.get(f"{self.list_url}?search=Al Wuheida")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["place_name"], "Al Wuheida")

    def test_retrieve_postal_code(self):
        """
        Verify that retrieving a postal code by ID works.
        """
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["place_name"], "Al Wuheida")

    def test_create_postal_code_superuser(self):
        """
        Verify that superuser can create a postal code.
        """
        payload = {
            "country_code": "IN",
            "postal_code": "110001",
            "place_name": "Connaught Place",
            "latitude": 28.6304,
            "longitude": 77.2177
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])
        self.assertTrue(PostalCode.objects.filter(postal_code="110001").exists())

    def test_dropdown_endpoint(self):
        """
        Verify that the dropdown selection endpoint functions correctly.
        """
        response = self.client.get(self.dropdown_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        results = response.data["data"]
        self.assertEqual(len(results), 2)
        # Check matching dropdown structure
        item = next(i for i in results if i["value"] == str(self.postal_dubai.id))
        self.assertEqual(item["label"], "32867 98458 - Al Wuheida (AE)")

    def test_nearest_coordinate_search(self):
        """
        Verify that the custom nearest coordinate search returns the closest postal code.
        """
        # Target coordinate closer to Dubai (25.29, 55.34)
        # Dubai coordinates: 25.2933, 55.3399
        # Abu Dhabi coordinates: 24.4686, 54.3413
        response = self.client.get(f"{self.nearest_url}?latitude=25.2900&longitude=55.3400")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["place_name"], "Al Wuheida")

        # Target coordinate closer to Abu Dhabi (24.47, 54.34)
        response = self.client.get(f"{self.nearest_url}?latitude=24.4700&longitude=54.3400")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["place_name"], "Al Khalidiya")

    def test_nearest_validation_errors(self):
        """
        Verify nearest endpoint parameter validations.
        """
        # Missing parameters
        response = self.client.get(self.nearest_url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Invalid float values
        response = self.client.get(f"{self.nearest_url}?latitude=abc&longitude=54.34")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Invalid range values (latitude too large)
        response = self.client.get(f"{self.nearest_url}?latitude=95.0&longitude=54.34")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_blocked(self):
        """
        Verify that unauthenticated users are blocked.
        """
        self.client.force_authenticate(user=None)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_standard_user_blocked_from_mutations(self):
        """
        Verify standard authenticated users can read, but are blocked from creating/deleting.
        """
        self.client.force_authenticate(user=self.user)

        # Reads should succeed
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Mutations should be blocked with 403 Forbidden
        payload = {
            "country_code": "IN",
            "postal_code": "110001",
            "place_name": "CP"
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_temp_user_authenticated_access(self):
        """
        Verify that a TempUser is authorized to read postal codes.
        """
        from onboard.models import TempUser
        temp_user = TempUser.objects.create(
            email="tempuser@example.com",
            first_name="Temp",
            last_name="User",
            business_name="Temp Corp"
        )
        self.client.force_authenticate(user=temp_user)

        # Reads should succeed
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Mutations should still be blocked (IsSuperUser fails for TempUser)
        payload = {
            "country_code": "IN",
            "postal_code": "110001",
            "place_name": "CP"
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_temp_user_token_authentication(self):
        """
        Verify that a TempUser can authenticate via token headers.
        """
        from onboard.models import TempUser
        from onboard.authentication import generate_temp_token
        temp_user = TempUser.objects.create(
            email="tempuser2@example.com",
            first_name="Temp",
            last_name="User",
            business_name="Temp Corp"
        )
        token = generate_temp_token(temp_user)
        
        # Clear authentication
        self.client.force_authenticate(user=None)
        
        # Make request with temporary token in headers
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_list_query_timeout(self):
        """
        Verify that a query timeout is gracefully handled and returns a 400 response.
        """
        from unittest.mock import patch
        from onboard.views.postal_code import QueryTimeoutException

        # Mock _execute_with_timeout to raise QueryTimeoutException directly
        with patch("onboard.views.postal_code.PostalCodeViewSet._execute_with_timeout") as mock_execute:
            mock_execute.side_effect = QueryTimeoutException()
            response = self.client.get(self.list_url)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertFalse(response.data["status"])
            self.assertEqual(
                response.data["message"],
                "Database query timed out. Please refine your search filters to narrow down results."
            )

    def test_dropdown_and_list_hard_limits(self):
        """
        Verify that list and dropdown endpoints enforce a hard limit of 100 records.
        """
        # Create 105 active postal codes
        postal_codes_to_create = []
        for i in range(105):
            postal_codes_to_create.append(
                PostalCode(
                    country_code="IN",
                    postal_code=f"1100{i:02d}",
                    place_name=f"Place {i}",
                    latitude=28.6304,
                    longitude=77.2177,
                    is_active=True
                )
            )
        # Bulk create them
        PostalCode.objects.bulk_create(postal_codes_to_create)

        # 1. Test dropdown endpoint (should return at most 100 records)
        response = self.client.get(self.dropdown_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 100)

        # 2. Test list endpoint with large page_size parameter (should be capped at 100)
        response = self.client.get(f"{self.list_url}?page_size=150")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 100)

    def test_postal_code_search_ignores_spaces(self):
        """
        Verify that searching and filtering by postal code ignores spaces.
        """
        # Test exact match filtering on postal_code (searching '3286798458' should match '32867 98458')
        response = self.client.get(f"{self.list_url}?postal_code=3286798458")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["place_name"], "Al Wuheida")

        # Test search query matching (searching '3286798458' should match '32867 98458')
        response = self.client.get(f"{self.list_url}?search=3286798458")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["place_name"], "Al Wuheida")

        # Test icontains query matching (searching '3286798' should match '32867 98458')
        response = self.client.get(f"{self.list_url}?postal_code__icontains=3286798")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["place_name"], "Al Wuheida")



