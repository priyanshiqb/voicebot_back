from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from onboard.models import TempUser

User = get_user_model()


class UserLoginRedirectionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.login_url = reverse("api-v1:login")

        # Create standard fully onboarded user
        self.active_user = User.objects.create_user(
            email="active.user@example.com",
            password="SecureActivePass123!",
            first_name="Active",
            last_name="User"
        )
        self.active_user.is_active = True
        self.active_user.save()

        # Create pending onboarding TempUser
        self.temp_user = TempUser.objects.create(
            email="pending.user@example.com",
            first_name="Pending",
            last_name="User",
            business_name="Pending Corp",
            password_hash=make_password("SecurePendingPass123!")
        )

    def test_active_user_login_success(self):
        """Verify that a fully registered active user logs in successfully."""
        payload = {
            "email": "active.user@example.com",
            "password": "SecureActivePass123!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertIn("access", response.data["data"])
        self.assertIn("refresh", response.data["data"])

    def test_active_user_login_wrong_password(self):
        """Verify that standard login fails with incorrect credentials."""
        payload = {
            "email": "active.user@example.com",
            "password": "WrongPassword123!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        self.assertIn("credentials", response.data["errors"])

    def test_pending_user_login_redirect(self):
        """Verify that login with correct credentials of a pending user returns onboarding redirection error."""
        payload = {
            "email": "pending.user@example.com",
            "password": "SecurePendingPass123!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        
        errors = response.data["errors"]
        self.assertTrue(errors.get("onboarding_pending"))
        self.assertEqual(errors.get("temp_registration_id"), str(self.temp_user.id))
        self.assertEqual(errors.get("email"), "pending.user@example.com")
        self.assertIn("registration is still pending", response.data["message"])

    def test_pending_user_login_wrong_password(self):
        """Verify that login with incorrect password for a pending user returns generic credentials error (securing email discovery)."""
        payload = {
            "email": "pending.user@example.com",
            "password": "WrongPendingPass!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        
        errors = response.data["errors"]
        self.assertNotIn("onboarding_pending", errors)
        self.assertIn("credentials", errors)
