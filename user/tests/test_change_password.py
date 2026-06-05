from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model

User = get_user_model()


class UserChangePasswordTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.change_password_url = reverse("api-v1:customuser-change-password")

        # Create test user
        self.user = User.objects.create_user(
            email="changepw@example.com",
            password="SecurePassword123!",
            first_name="Change",
            last_name="Password"
        )
        self.user.is_active = True
        self.user.save()

    def test_unauthenticated_access_denied(self):
        """Verify that accessing change password endpoint without authentication fails with 401."""
        response = self.client.post(self.change_password_url, {})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_password_success(self):
        """Verify that user can change password successfully with valid current and new password."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "old_password": "SecurePassword123!",
            "new_password": "NewSecurePassword123!",
            "confirm_password": "NewSecurePassword123!"
        }
        response = self.client.post(self.change_password_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["message"], "Password changed successfully.")

        # Try to authenticate/check password with new password
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewSecurePassword123!"))

    def test_change_password_invalid_old_password(self):
        """Verify that changing password with incorrect old password fails."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "old_password": "WrongPassword123!",
            "new_password": "NewSecurePassword123!",
            "confirm_password": "NewSecurePassword123!"
        }
        response = self.client.post(self.change_password_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("old_password", response.data["errors"])

    def test_change_password_mismatch(self):
        """Verify that changing password fails if new password and confirm password do not match."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "old_password": "SecurePassword123!",
            "new_password": "NewSecurePassword123!",
            "confirm_password": "DifferentPassword123!"
        }
        response = self.client.post(self.change_password_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("confirm_password", response.data["errors"])

    def test_change_password_same_as_old(self):
        """Verify that new password cannot be the same as old password."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "old_password": "SecurePassword123!",
            "new_password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.change_password_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.data["errors"])

    def test_change_password_weak_password(self):
        """Verify that weak new password fails complexity checks."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "old_password": "SecurePassword123!",
            "new_password": "weak",
            "confirm_password": "weak"
        }
        response = self.client.post(self.change_password_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.data["errors"])
